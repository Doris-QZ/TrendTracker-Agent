import arxiv
import asyncio
import httpx
import time
import random
import logging
import pandas as pd
import numpy as np
from typing import Optional
from tavily import AsyncTavilyClient
from datetime import datetime, timezone


logger = logging.getLogger(__name__)

# Asynchronous tavily search
async def tavily_search(
    tavily_client: AsyncTavilyClient, 
    semaphore: asyncio.Semaphore,
    query: str, 
    max_retries: int = 3):
    """Run asynchronous search on tavily with retry logic for rate limits."""
    for attempt in range(max_retries + 1):
        try:
            async with semaphore:
                await asyncio.sleep(random.uniform(0.1, 0.5))
                response = await tavily_client.search(query, max_results=5)

            # Check for rate limit in response
            if "detail" in response and "error" in response['detail']:
                error_msg = response['detail']['error']
                if "excessive" in error_msg.lower():
                    raise Exception(f"RateLimit: {error_msg}")
            
            # Success
            return response.get('results', [])

        except Exception as e:
            error_msg = str(e).lower()

            # Retry on rate limit errors, but log and skip on other errors
            if "excessive" in error_msg or "ratelimit" in error_msg:
                wait_time = 1.0 * (attempt + 1)
                logger.warning(f"Tavily Rate Limit hit. Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue
            else:
                logger.error(f"Tavily Search failed for '{query}': {error_msg}")
                return []

    logger.error(f"Max retries reached for Tavily Search")
    return []


# Arxiv search
def run_sync_arxiv_search(query: str):
    """Run arxiv search synchronously."""
    # Define the client
    client = arxiv.Client(page_size=1000, delay_seconds=3.0)

    # Search Configuration
    search = arxiv.Search(
        query=query,
        max_results=2000,
        sort_by=arxiv.SortCriterion.Relevance,
        sort_order=arxiv.SortOrder.Descending,
    )

    arxiv_papers = []
    for result in client.results(search):
        try:
            short_id = result.get_short_id().split('v')[0]
        except Exception:
            short_id = result.entry_id.split('/')[-1].split('v')[0]

        arxiv_papers.append({
            'arxiv_id': short_id,
            'title': result.title,
            'abstract': result.summary,
            'url': result.entry_id,
            'published_date': result.published,
        })

    return arxiv_papers


# Helper function to fetch a single batch of semantic scholar metrics
async def _fetch_s2_batch(  
    batch_ids: list[str],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    headers: dict,
    max_retries: int = 3
) -> list[dict]:
    """
    Fetches a single batch of semantic scholar metrics.
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/batch"
    params = {"fields": "citationCount,influentialCitationCount,authors.hIndex"} 
    payload = {"ids": [f"ARXIV:{arxiv_id}" for arxiv_id in batch_ids]}

    # Retry logic loop
    for attempt in range(max_retries + 1):
        try:
            async with semaphore:
                response = await client.post(
                    url,
                    params=params,
                    json=payload,
                    headers=headers,
                )

            # Success
            if response.status_code == 200:
                data = response.json()
                batch_records = []

                for arxiv_id, item in zip(batch_ids, data):
                    if item is None:
                        continue

                    authors = item.get('authors', [])
                    h_indices = [a.get('hIndex') for a in authors if a.get('hIndex') is not None]

                    max_h = max(h_indices) if h_indices else 0
                    mean_h = np.mean(h_indices) if h_indices else 0

                    batch_records.append({
                        'arxiv_id': arxiv_id,
                        'citations': item.get('citationCount', 0),
                        'influential_citations': item.get('influentialCitationCount', 0),
                        'author_score': max_h + 0.5 * mean_h
                    })

                return batch_records

            # Client error (non-retryable)
            if 400 <= response.status_code < 429:
                logger.error(f"S2 Client Error: {response.status_code}")
                return []

            # Calculate wait time
            wait_time = 1.0 * (attempt + 1)

            # Rate Limiting (429)
            if response.status_code == 429:
                wait_time = response.headers.get("Retry-After", wait_time)
                logger.warning(f"S2 Rate limit hit. Retrying in {wait_time}s...")

            # Server Errors (5xx)
            elif response.status_code >= 500:
                logger.warning(f"S2 Server Error {response.status_code}. Retrying...")

            await asyncio.sleep(wait_time)

        except httpx.RequestError as exc:
            logger.error(f"S2 Request failed (Attempt {attempt+1}): {exc}")
            await asyncio.sleep(1.0 * (attempt + 1))

    logger.error("Max retries reached for S2 batch.")
    return []


# Fetch semantic scholar metrics
async def fetch_s2_metrics(
    arxiv_ids: list[str],
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    headers: dict,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    Fetches citations, influential citations and author h-index from Semantic Scholar.
    Returns a DataFrame.
    """
    BATCH_SIZE = 500

    # Create coroutines for each batch
    tasks = [
        _fetch_s2_batch(arxiv_ids[i:i+BATCH_SIZE], client, semaphore, headers, max_retries)
        for i in range(0, len(arxiv_ids), BATCH_SIZE)
    ]

    # Run all batches concurrently up to the semaphore limit
    results = await asyncio.gather(*tasks, return_exceptions=True)

    records = []
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"S2 Batch failed: {res}")
        elif isinstance(res, list):
            records.extend(res)
        else:
            logger.warning(f"Unexpected S2 result type: {type(res)}")

    return pd.DataFrame(records)


# HF helper 1: fetch upvotes and githubStars of a single paper
async def _fetch_paper_metadata(
    arxiv_id: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    headers: dict,
    max_retries: int = 3,
) -> dict:
    """
    Fetches upvotes and githubStars of a single paper from HuggingFace.
    """
    # API Endpoints
    paper_url = f"https://huggingface.co/api/papers/{arxiv_id}"

    # Retry logic
    for attempt in range(max_retries + 1):
        try:
            async with semaphore:
                paper_resp = await client.get(paper_url, headers=headers, timeout=10.0)

            # Success
            if paper_resp.status_code == 200:
                data = paper_resp.json()
                return {
                    "arxiv_id": arxiv_id,
                    "hf_upvotes": data.get("upvotes", 0),
                    "github_stars": data.get("githubStars", 0),
                    "paper_not_found": 0
                }

            # Paper not indexed on HF
            elif paper_resp.status_code == 404:
                return {"arxiv_id": arxiv_id, "hf_upvotes": 0, "github_stars": 0, "paper_not_found": 1}

            # Rate Limit Handling (429)
            elif paper_resp.status_code == 429:
                wait_time = 10 * (attempt + 1)
                logger.warning(f"HF Paper API Rate Limit. Cooling down for {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue

        except httpx.RequestError as exc:
            logger.error(f"HF Paper API Request failed for {arxiv_id}. Retrying..")
            await asyncio.sleep(1.0 * (attempt + 1))

    return {"arxiv_id": arxiv_id, "hf_upvotes": 0, "github_stars": 0, "paper_not_found": 1}

# HF helper 2: fetch model citations of a single paper
last_model_request_time = [0.0]

async def _fetch_model_references(
    arxiv_id: str,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    headers: dict,
    max_retries: int = 3,
) -> dict:
    """
    Fetches model references of a single paper from HuggingFace.
    """
    # API Endpoints
    models_url = "https://huggingface.co/api/models"
    CADENCE = 1.0  # Minimum time between model API calls in seconds

    for attempt in range(max_retries + 1):
        now = time.monotonic()
        elapsed = now - last_model_request_time[0]

        if elapsed < CADENCE:
            wait_time = CADENCE - elapsed + random.uniform(0.1, 0.3)
            await asyncio.sleep(wait_time)

        try:
            async with semaphore:
                last_model_request_time[0] = time.monotonic()
                model_params = {"filter": f"arxiv:{arxiv_id}", "limit": 1000}
                model_resp = await client.get(models_url, params=model_params, headers=headers, timeout=30.0)

            # Success
            if model_resp.status_code == 200:
                data = model_resp.json()
                if isinstance(data, list):
                    return {"arxiv_id": arxiv_id, "hf_model_references": len(data)}
                else:
                    return {"arxiv_id": arxiv_id, "hf_model_references": 0}

            elif model_resp.status_code == 404:
                return {"arxiv_id": arxiv_id, "hf_model_references": 0}

            # Rate Limit Handling (429)
            elif model_resp.status_code == 429:
                retry_after = model_resp.headers.get("Retry-After")

                if retry_after:
                    wait_time = float(retry_after) + 0.5
                else:
                    wait_time = (5 * (2 ** attempt)) + random.uniform(0, 2)

                logger.warning(f"HF Model API Rate Limit. Cooling down for {wait_time}s...")
                await asyncio.sleep(wait_time)
                continue

        except httpx.RequestError as exc:
            logger.error(f"HF Model API Request failed for {arxiv_id}. Retrying..")
            await asyncio.sleep(1.0 * (attempt + 1))

    return {"arxiv_id": arxiv_id, "hf_model_references": 0}


# Fetch HuggingFace metrics
async def fetch_hf_metrics(
    arxiv_ids: list[str],
    client: httpx.AsyncClient,
    paper_semaphore: asyncio.Semaphore,
    model_semaphore: asyncio.Semaphore,
    headers: dict,
    max_retries: int = 3,
) -> pd.DataFrame:

    PAPER_BATCH = 50
    MODEL_BATCH = 10
    BATCH_COOLDOWN = 1.0

    # Fetch paper metadata in batches
    hf_papers = []
    for i in range(0, len(arxiv_ids), PAPER_BATCH):
        batch_ids = arxiv_ids[i : i + PAPER_BATCH]

        tasks = [
            _fetch_paper_metadata(arxiv_id, client, paper_semaphore, headers, max_retries)
            for arxiv_id in batch_ids
        ]

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in batch_results:
            if isinstance(res, dict):
                hf_papers.append(res)
            else:
                logger.error(f"HF Batch Error: {res}")

        # Sleep between batches
        if i + PAPER_BATCH < len(arxiv_ids):
            await asyncio.sleep(BATCH_COOLDOWN)

    # Extract arxiv_ids that were found on HF for model citation fetching
    found_arxiv_ids = [p['arxiv_id'] for p in hf_papers if p['paper_not_found'] == 0]

    # Fetch model citations in batches
    if found_arxiv_ids:
        hf_models = []
        for i in range(0, len(found_arxiv_ids), MODEL_BATCH):
            batch_ids = found_arxiv_ids[i : i + MODEL_BATCH]

            tasks = [
                _fetch_model_references(arxiv_id, client, model_semaphore, headers, max_retries)
                for arxiv_id in batch_ids
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in batch_results:
                if isinstance(res, dict):
                    hf_models.append(res)
                else:
                    logger.error(f"HF Model Batch Error: {res}")

            # Sleep between batches
            if i + MODEL_BATCH < len(arxiv_ids):
                await asyncio.sleep(BATCH_COOLDOWN)

        # Merge paper metadata and model citations on arxiv_id
        hf_metrics = pd.DataFrame(hf_papers).merge(pd.DataFrame(hf_models), on='arxiv_id', how='left')

        # Fill NaN values with 0 (if model citations exist but paper metadata doesn't, we assume paper is found)
        cols = ['hf_upvotes', 'github_stars', 'hf_model_references', 'paper_not_found']
        for col in cols:
            hf_metrics[col] = hf_metrics[col].fillna(0)
    else:
         hf_metrics = pd.DataFrame()

    return hf_metrics


# Calcuate velocity of given metrics
def calculate_velocity(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate the velocity of given metrics for papers < 90 days old.
    """
    # Create a working copy
    velocity_df = df.copy()

    velocity_df['published_date'] = pd.to_datetime(velocity_df['published_date'], utc=True)
    current_date = pd.Timestamp.now('UTC')
    diff = current_date - velocity_df['published_date']
    velocity_df['days'] = diff.dt.days

    fast_score = ['hf_upvotes', 'github_stars']
    slow_score = ['citations', 'influential_citations', 'hf_model_references']

    for metric in fast_score:
        velocity_df[f'{metric}_velocity'] = velocity_df[metric] / pow(velocity_df['days']+1, 0.5)

    for metric in slow_score:
        velocity_df[f'{metric}_velocity'] = velocity_df[metric] / pow(velocity_df['days']+1, 0.3)

    return velocity_df


# Calculate final score
def calculate_final_score(df: pd.DataFrame, category: str) -> pd.DataFrame:
    """
    Calculates the final score of papers based on the category. Returns a DataFrame.
    """
    # papers > 90 days old
    if category == 'impact':
        weights = {
        # Universal anchors 20%
        'relevance_score': 0.15,
        'author_score': 0.05,

        # Academic validity 50%
        'influential_citations_stability': 0.30,
        'citations_stability': 0.20,

        # Engineering utility 30%
        'hf_model_references_stability': 0.15,
        'github_stars_stability': 0.10,
        'hf_upvotes_stability': 0.05
    }

    # papers between 30 and 90 days old
    elif category == 'momentum':
        weights = {
        # Universal anchors 50%
        'relevance_score': 0.15,
        'author_score': 0.20,
        'novelty_score': 0.10,
        'concept_match': 0.05,

        # Academic validity 30%
        'influential_citations_velocity': 0.20,
        'citations_velocity': 0.10,

        # Engineering Utility 20%
        'hf_model_references_velocity': 0.10,
        'github_stars_velocity': 0.05,
        'hf_upvotes_velocity': 0.05,
    }

    # papers < 30 days old
    else:
        weights = {
            # Universal anchors 80%
            'relevance_score': 0.25,
            'author_score': 0.30,
            'novelty_score': 0.10,
            'concept_match': 0.15,
            
            # Academic validity 5%
            'influential_citations_velocity': 0.025,
            'citations_velocity': 0.025,

            # Engineering Utility 15%
            'hf_model_references_velocity': 0.05,
            'github_stars_velocity': 0.05,
            'hf_upvotes_velocity': 0.05,
        }

    # Create a working copy
    score_df = df.copy()

    # Track normalization columns to drop later
    norm_cols = []

    # Normalize metrics
    for metric in weights.keys():
        col_name = f'norm_{metric}'
        norm_cols.append(col_name)

        if score_df[metric].sum() == 0:
            score_df[col_name] = 0.0
        else:
            score_df[col_name] = score_df[metric].rank(pct=True)

    # Calculate Weighted Sum
    score_df['final_score'] = 0.0
    for metric, weight in weights.items():
        score_df['final_score'] += score_df[f'norm_{metric}'] * weight

    # Drop the normalization columns
    score_df.drop(columns=norm_cols, inplace=True)

    return score_df.sort_values(by='final_score', ascending=False)