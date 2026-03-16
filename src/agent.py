# region === Imports & Initialization ===
import os
import sys
import logging
from dotenv import load_dotenv
import warnings

# 1. Setup Environment
load_dotenv()

# 2. Configure Logging
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True
)
logger = logging.getLogger("trend_tracker")
logger.setLevel(logging.INFO)
logging.getLogger("src").setLevel(logging.INFO)

# 3. Silence Pydantic Serialization Warnings
# Note: These are caused by LangChain's internal metadata wrapping in Pydantic v2
warnings.filterwarnings("ignore", message="Pydantic serializer warnings")

# 4. Foundation Packages 
import re
import httpx
import asyncio
import numpy as np
import pandas as pd
from operator import add
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional, TypedDict
from pydantic import BaseModel, Field, field_validator

# 5. AI Agent Packages 
from tavily import AsyncTavilyClient
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, get_buffer_string
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Command, Send, interrupt
from sentence_transformers import SentenceTransformer

# 6. Local Imports 
from src import prompts
from src import utils

# endregion

# region === State Schema ===

# Schema of the industry analyst
class Analyst(BaseModel):
    name: str = Field(description="A unique semantic identifier in snake_case")
    expertise: str = Field(description="A specialized AI/ML domain lens" )
    research_task: str = Field(description="A trend-oriented mission starting with an action verb.")

    @property
    def persona(self) -> str:
        return (f"name: {self.name}\nexpertise: {self.expertise}\nresearch_task: {self.research_task}")


# Schema of Industry Interview SubGraph 
# Schema of the industry analyst
class Analyst(BaseModel):
    name: str = Field(description="A unique semantic identifier in snake_case")
    expertise: str = Field(description="A specialized AI/ML domain lens" )
    research_task: str = Field(description="A trend-oriented mission starting with an action verb.")

    @property
    def persona(self) -> str:
        return (f"name: {self.name}\nexpertise: {self.expertise}\nresearch_task: {self.research_task}")


# Industry Interview SubGraph Schema
MAX_NUM_TURNS = 5
class IndustryInterviewState(MessagesState):
    analyst: Analyst
    max_num_turns: int      
    search_results: str
    interview_memos: Annotated[list[str], add]


# Academic Research SubGraph Schema
class AcademicResearchState(TypedDict):
    topic: str
    query: str
    arxiv_papers: list[dict]
    impact_papers: list[dict]
    momentum_papers: list[dict]
    latest_papers: list[dict]
    research_summaries: Annotated[list[str], add]


# Academic Research Output Schema
class AcademicResearchOutput(TypedDict):
    impact_papers: list[dict]
    momentum_papers: list[dict]
    latest_papers: list[dict]
    research_summaries: Annotated[list[str], add]


# Overall State Schema
class TrendTrackerState(TypedDict):
    topic: str
    topic_clarification: Annotated[str, add]
    research_plan: dict
    plan_review: dict
    planner_retry_count: Annotated[int, add]
    human_feedback: str
    industry_analysts: list[Analyst]
    arxiv_queries: list[str]
    interview_memos: Annotated[list[str], add]
    research_summaries: Annotated[list[str], add]
    blog_critique: str
    blogger_retry_count: Annotated[int, add]
    tech_blog: str

# endregion

# region === Models ===

# Chat models
frontier_llm = ChatOpenAI(model_name="gpt-5.2")         # complex reasoning & judgment
standard_llm = ChatOpenAI(model_name="gpt-5")           # balanced performance & cost  
efficient_llm = ChatOpenAI(model_name="gpt-5-mini")     # lightweight & fast tasks

# Sentence embedding model
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# Tavily client
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
tavily_client = AsyncTavilyClient(api_key=TAVILY_API_KEY)

# endregion

# region === Part I Research Plan ===

### === Research Planner ===
# Schema of a research angle
class Perspective(BaseModel):
    category: Literal["academic", "industry"] = Field(
        description="Whether this research angle targets academic research (ArXiv) or industry sources (web search)."
    )
    research_angle: str = Field(
        description="A concise, specific research angle that can be independently investigated."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Model-estimated confidence that this angle is well-scoped and relevant."
    )


# Schema of research angles
class ResearchPlan(BaseModel):
    questions: str | None
    research_angles: list[Perspective] = Field(
        description="A list of research angles that can be independently investigated."
    )


# LLM with structured output
research_planner_llm = frontier_llm.with_structured_output(ResearchPlan)


# Node to generate research angles or ask questions
def research_planner(state: TrendTrackerState):
    """Generates research angles or ask questions."""
    # Get state
    topic = state['topic']
    topic_clarification = state.get('topic_clarification') or 'N/A'

    research_plan = state.get('research_plan', {})
    if research_plan and research_plan.get('research_angles'):
        angles = research_plan['research_angles']
        research_angles = "\n\n".join([f"index: Angle_{i}\n"
                                    f"category: {a['category']}\n"
                                    f"research_angle: {a['research_angle']}\n"
                                    f"confidence: {a['confidence']}"
                                    for i, a in enumerate(angles, start=1)])
    else:
        research_angles = 'N/A'

    plan_review = state.get('plan_review', {})
    reviewer_feedback = plan_review.get('reviewer_feedback') or 'N/A'

    human_feedback = state.get('human_feedback') or 'N/A'

    has_existing_angles = research_angles != 'N/A'

    # Human message
    human_msg = prompts.research_planner_human.format(
        topic=topic,
        topic_clarification=topic_clarification,
        research_angles=research_angles,
        reviewer_feedback=reviewer_feedback,
        human_feedback=human_feedback,
        has_existing_angles=has_existing_angles,
    )

    # Run
    planner_response = research_planner_llm.invoke([SystemMessage(content=prompts.research_planner_system)] +
                                                   [HumanMessage(content=human_msg)])

    return {'research_plan': planner_response.model_dump(),
            'planner_retry_count': 1}


# Node to implement routing logic after research planner
def plan_router (state: TrendTrackerState) -> Command[Literal['research_planner', 'plan_reviewer',
                                                              'create_analysts', 'create_arxiv_query']]:
    """Handles routing logic after research planner"""
    # Get state
    research_plan = state['research_plan']
    questions = research_plan.get('questions', None)
    angles = research_plan.get('research_angles', [])

    if angles:
        research_angles = "\n\n".join([f"index: Angle_{i}\n"
                                    f"category: {a['category']}\n"
                                    f"research_angle: {a['research_angle']}\n"
                                    f"confidence: {a['confidence']}"
                                    for i, a in enumerate(angles, start=1)])


    retry_count = state['planner_retry_count']
    human_feedback = state.get('human_feedback', '')

    # CASE 1: Planner needs clarification
    if not angles and questions:
        clarification = interrupt({
            'action': "Answer the questions below regarding the research topic:",
            'questions': questions
        })

        topic_clarification = f"Questions: {questions}\nClarification: {clarification}\n"

        return Command(update={'topic_clarification': topic_clarification},
                        goto='research_planner')

    # CASE 2: Human review required
    elif human_feedback or retry_count >= 3:
        human_feedback = interrupt({
            'action': "Review the research angles and provide feedback. Reply 'approve' if no editing needed.",
            'research_angles': research_angles
        })

        if human_feedback.lower() == 'approve':
            return Command(goto=['create_analysts', 'create_arxiv_query'])
        else:
            return Command(update={'human_feedback': human_feedback},
                           goto='research_planner')

    # CASE 3: Default path - proceed to automated reviewer
    else:
        return Command(goto='plan_reviewer')


### === Plan Reviewer ===
# Schema of PlanReview
class PlanReview(BaseModel):
    is_valid: bool = Field(
        description="True if all angles meet the criteria. False if edits are required."
    )
    reviewer_feedback: str | None = Field(
        description="Specific instructions for the planner if any research angle is invalid. Leave null if valid."
    )
    requires_human: bool = Field(
        description="Set to True if the research angles are technically valid but the researcher confidence is too low (<0.6)."
    )


# LLM with structured output
plan_reviewer_llm = standard_llm.with_structured_output(PlanReview)


# Node to review the research angles
def plan_reviewer(state: TrendTrackerState):
    """Reviews the research angles"""
    # Get state
    topic = state['topic']
    angles = state['research_plan']['research_angles']
    research_angles = "\n\n".join([f"index: Angle_{i}\n"
                                    f"category: {a['category']}\n"
                                    f"research_angle: {a['research_angle']}\n"
                                    f"confidence: {a['confidence']}"
                                    for i, a in enumerate(angles, start=1)])
    previous_review = state.get('plan_review', {})
    reviewer_feedback = previous_review.get('reviewer_feedback') or 'N/A'

    # Human message
    human_msg = prompts.plan_reviewer_human.format(
        topic=topic,
        research_angles=research_angles,
        reviewer_feedback=reviewer_feedback,
    )

    # Run
    new_review = plan_reviewer_llm.invoke([SystemMessage(content=prompts.plan_reviewer_system)] +
                                          [HumanMessage(content=human_msg)])

    return {'plan_review': new_review.model_dump()}


# Node to implement routing logic after plan reviewer
def review_router(state: TrendTrackerState)-> Command[Literal['research_planner', 'create_analysts',
                                                              'create_arxiv_query']]:
    """Implements routing logic after plan reviewer"""
    # Get state
    review = state['plan_review']
    angles = state['research_plan']['research_angles']
    angles_count = len(angles)
    research_angles = "\n\n".join([f"index: Angle_{i}\n"
                                    f"category: {a['category']}\n"
                                    f"research_angle: {a['research_angle']}\n"
                                    f"confidence: {a['confidence']}"
                                    for i, a in enumerate(angles, start=1)])

    # CASE 1: Human review required
    if review.get('requires_human') or (review.get('is_valid') and angles_count > 6):
        human_feedback = interrupt({
            'action': "Review the research angles and provide feedback. Reply 'approve' if no editing needed.",
            'research_angles': research_angles
        })

        if human_feedback.lower() == 'approve':
            return Command(goto=['create_analysts', 'create_arxiv_query'])
        else:
            return Command(update={'human_feedback': human_feedback},
                           goto='research_planner')

    # CASE 2: Auto approved
    elif review.get('is_valid'):
        return Command(goto=['create_analysts', 'create_arxiv_query'])

    # CASE 3: Reject plan, loop back to planner
    else:
        return Command(goto='research_planner')


### === Create Industry Analysts ===
# Schema for create analysts
class IndustryAnalysts(BaseModel):
    industry_analysts: list[Analyst] = Field(description="List of industry analysts.")


# Schema for analysts review
class AnalystReview(BaseModel):
    is_valid: bool = Field(description="True if the analyst list meets all criteria, False otherwise.")
    reviewer_feedback: str | None = Field(
        description="Detailed, actionable feedback for the editor. Null if is_valid is True."
    )


# LLM with structured output
create_analysts_llm = standard_llm.with_structured_output(IndustryAnalysts)
analysts_reviewer_llm = standard_llm.with_structured_output(AnalystReview)


# Node to create industry analysts
def create_analysts(state: TrendTrackerState):
    """Creates a set of industry analysts."""
    # Get state
    topic = state['topic']
    angles = state['research_plan']['research_angles']
    industry_list = [a for a in angles if a['category']=='industry']
    industry_research_angles = "\n".join([f"{i}. {a['research_angle']}"
                                          for i, a in enumerate(industry_list, start=1)])

    # Human message
    human_msg = prompts.create_analysts_human.format(
        topic=topic,
        industry_research_angles=industry_research_angles,
    )

    # Generate analysts
    current_analysts = create_analysts_llm.invoke(
        [SystemMessage(content=prompts.create_analysts_system)] +
        [HumanMessage(content=human_msg)]
        ).industry_analysts

    # Review and edit the analysts
    max_retries = 2
    attempts = 0
    reviewer_feedback = ''

    while attempts < max_retries:
        # Review
        industry_analysts="\n\n".join([
            f"index: Analyst_{i}\n{analyst.persona}"
            for i, analyst in enumerate(current_analysts, start=1)])

        review_human_msg = prompts.review_analysts_human.format(
            industry_analysts=industry_analysts,
            industry_research_angles=industry_research_angles,
            reviewer_feedback=reviewer_feedback,
        )

        review_result = analysts_reviewer_llm.invoke(
            [SystemMessage(content=prompts.review_analysts_system)] +
            [HumanMessage(content=review_human_msg)]
            )

        # Exit if valid
        if review_result.is_valid:
            return {'industry_analysts': current_analysts}

        # Edit if invalid
        reviewer_feedback = review_result.reviewer_feedback

        editor_human_msg = prompts.edit_analysts_human.format(
            industry_research_angles=industry_research_angles,
            industry_analysts=industry_analysts,
            reviewer_feedback=reviewer_feedback,
        )

        current_analysts = create_analysts_llm.invoke(
            [SystemMessage(content=prompts.edit_analysts_system)] +
            [HumanMessage(content=editor_human_msg)]
            ).industry_analysts

        attempts += 1

    # if loop finishes without is_valid=True, return the last edited version
    return {'industry_analysts': current_analysts}


### === Create Arxiv Queries ===
# Schema of a single ArXiv search query
class Query(BaseModel):
    arxiv_query: str = Field(
        description="The valid ArXiv search string. strictly do NOT use backslashes to escape quotes"
    )

    @field_validator("arxiv_query")
    @classmethod
    def clean_query(cls, v: str) -> str:
        # Remove escaped quotes from the ArXiv query string.
        v = v.replace('\\"', '"').replace('\"', '"')

        # Remove trailing text after the submittedDate block
        match = re.search(r"(submittedDate:\[\d{12}\s+TO\s+\d{12}\])", v)
        if match:
            v = v[:match.end()]
        
        # Remove illegal characters that may cause ArXiv search failure
        v = re.sub(r'[{}?]', '', v)

        # Remove excessive whitespace
        v = re.sub(r'\s+', ' ', v).strip()

        return v


# Schema of arxiv queries
class ArxivQueries(BaseModel):
    arxiv_queries: list[Query] = Field(description="A list of ArXiv search queries.")


# Schema for query review
class QueryReview(BaseModel):
    is_valid: bool = Field(description="True if the ArXiv search queries meets all criteria, False otherwise.")
    reviewer_feedback: str | None = Field(
        description="Detailed, actionable feedback for the editor. Null if is_valid is True."
    )


# LLM with structured output
arxiv_query_llm = standard_llm.with_structured_output(ArxivQueries)
query_reviewer_llm = standard_llm.with_structured_output(QueryReview)


# Node to generate ArXiv search queries
def create_arxiv_query(state: TrendTrackerState):
    """Creates a set of ArXiv search queries."""
    # Get state
    topic = state['topic']
    angles = state['research_plan']['research_angles']
    academic_list = [a for a in angles if a['category']=='academic']
    academic_research_angles = "\n".join([f"{i}. {a['research_angle']}"
                                          for i, a in enumerate(academic_list, start=1)])
    current_datetime = datetime.now().strftime("%Y%m%d%H%M")

    # Human message
    human_msg = prompts.arxiv_query_human.format(
        topic=topic,
        academic_research_angles=academic_research_angles,
        current_datetime=current_datetime,
    )

    # Create queries
    current_queries = arxiv_query_llm.invoke(
        [SystemMessage(content=prompts.arxiv_query_system)] +
        [HumanMessage(content=human_msg)]
        ).arxiv_queries

    # Review and edit the queries
    max_retries = 2
    attempts = 0
    reviewer_feedback = 'N/A'

    while attempts < max_retries:
        # Review
        arxiv_queries = "\n".join([f"Query_{i}: {query.arxiv_query}"
                                    for i, query in enumerate(current_queries, start=1)])

        review_human_msg = prompts.query_review_human.format(
            topic=topic,
            academic_research_angles=academic_research_angles,
            arxiv_queries=arxiv_queries,
            reviewer_feedback=reviewer_feedback,
            current_datetime=current_datetime,
        )

        review_result = query_reviewer_llm.invoke(
            [SystemMessage(content=prompts.query_review_system)] +
            [HumanMessage(content=review_human_msg)]
            )

        # Exit if valid
        if review_result.is_valid:
            queries = [q.arxiv_query for q in current_queries]
            return {'arxiv_queries': queries}

        # Edit if invalid
        reviewer_feedback = review_result.reviewer_feedback

        editor_human_msg = prompts.query_edit_human.format(
            topic=topic,
            academic_research_angles=academic_research_angles,
            arxiv_queries=arxiv_queries,
            reviewer_feedback=reviewer_feedback,
            current_datetime=current_datetime,
        )

        current_queries = arxiv_query_llm.invoke(
            [SystemMessage(content=prompts.query_edit_system)] +
            [HumanMessage(content=editor_human_msg)]
            ).arxiv_queries

        attempts += 1

    # if loop finishes without is_valid=True, return the last edited version
    queries = [q.arxiv_query for q in current_queries]
    return {'arxiv_queries': queries}


# Node to dispatch research tasks
def dispatch_research(state: TrendTrackerState):
    """No-op node that dispatches research tasks to specialized subgraphs."""
    pass


# Conditional routing logic to fan out research tasks to specialized subgraphs
def dispatch_router(state: TrendTrackerState) -> Literal['industry_research_subgraph',
                                                           'academic_research_subgraph']:
    """Conditional routing logic that fans out research tasks to specialized subgraphs."""
    # Get state
    topic = state['topic']
    analysts = state['industry_analysts']
    arxiv_queries = state['arxiv_queries']

    print(f"Creating {len(analysts)} industry research tasks")
    print(f"Creating {len(arxiv_queries)} academic research tasks")
   
    # Send analysts to industry research
    industry_research = [
        Send('industry_research_subgraph',
             {'analyst': analyst, 'max_num_turns': MAX_NUM_TURNS,
              'messages': [HumanMessage(content=f"So you said you were writing an article on {topic}")]})
        for analyst in analysts
    ]

    # Send arxiv queries to academic research
    academic_research = [
        Send('academic_research_subgraph', {'topic': topic, 'query': query})
        for query in arxiv_queries
    ]

    # Fan out
    return industry_research + academic_research
    
# endregion

# region === Part II Industry Research SubGraph ===

# Node to generate interview questions
def interview_question(state: IndustryInterviewState):
    """Generates interview questions for a given research task."""
    # Get state   
    messages = state['messages']  
    expertise = state['analyst'].expertise
    research_task = state['analyst'].research_task

    # Human message
    human_msg = prompts.generate_question_human.format(
        expertise=expertise,
        research_task=research_task,
    )

    # Generate a question
    question = efficient_llm.invoke(
        [SystemMessage(content=prompts.generate_question_system)] +
        [HumanMessage(content=human_msg)] + 
        messages
    )

    # Write messages to state
    return {'messages': [question]}


### web search
# Schema for search_query
class search_query(BaseModel):
    search_query: str = Field(description="A web search query.")


# LLM with structured output
search_query_llm = efficient_llm.with_structured_output(search_query)


# Semaphore for Tavily Search: Allows 3 concurrent HTTP requests across all subgraphs
_TAVILY_SEMAPHORE = None

def get_tavily_semaphore() -> asyncio.Semaphore:
    global _TAVILY_SEMAPHORE
    if _TAVILY_SEMAPHORE is None:
        _TAVILY_SEMAPHORE = asyncio.Semaphore(3)
    return _TAVILY_SEMAPHORE

# Node to implement web search using tavily
async def web_search(state: IndustryInterviewState):
    """Implements web search for a given interview question."""
    # Generate search query
    query = await search_query_llm.ainvoke(
        [SystemMessage(content=prompts.search_query_prompt)] + state['messages']
        )   
    search_query = query.search_query

    # Web search
    results = await utils.tavily_search(
        tavily_client=tavily_client, 
        semaphore=get_tavily_semaphore(),
        query=search_query, 
        max_retries = 3
    )

    # Format search results   
    formatted_docs = [
        f"<Document source='{doc.get('url', 'Unknown')}'>\n"
        f"Title: {doc.get('title', 'No title')}\n"       
        f"Content: {doc.get('content', 'No content available')}\n"
        f"</Document>"
        for doc in results if isinstance(doc, dict)
    ]
    
    search_results = "\n\n".join(formatted_docs)

    if not search_results:
        logger.error(f"No results found for {search_query}")
        search_results = "No relevant web results found."

    # Write messages to state
    return {'search_results': search_results}


# Node to generate answer
def generate_answer(state: IndustryInterviewState):
    """Generate an answer to the analyst's latest question."""
    # Get state  
    research_task = state['analyst'].research_task
    search_results = state['search_results']
    messages = state['messages']

    # Human message
    human_msg = prompts.generate_answer_human.format(
        research_task=research_task,
        search_results=search_results,
    )

    # Generate an answer
    answer = efficient_llm.invoke(
        [SystemMessage(content=prompts.generate_answer_system)] +
        [HumanMessage(content=human_msg)] + 
        messages
    )
    answer.name = 'expert'

    # Write messages to state
    return {'messages': [answer]}


# Conditional routing logic after generate_answer node
def router_after_answer(state: IndustryInterviewState) -> Literal['interview_question',
                                                                  'write_interview_memo']:
    """Conditional routing logic after generate_answer node"""
    # Get state
    messages = state['messages']
    max_num_turns = state['max_num_turns']
    last_inteview_question = messages[-2].content
    num_turns = len([m for m in messages if m.name == 'expert'])

    if num_turns >= max_num_turns:
        return 'write_interview_memo'
    elif "Thank you so much for your help" in last_inteview_question:
        return 'write_interview_memo'
    else:
        return 'interview_question'


# Node to write interview memo
def write_interview_memo(state: IndustryInterviewState):
    """Writes interview memo."""
    # Get state
    research_task = state['analyst'].research_task
    messages = state['messages']
    interview_notes = get_buffer_string(messages)

    # Human message
    human_msg = prompts.write_memo_human.format(
        research_task=research_task,
        interview_notes=interview_notes,
    )

    # Write interview memo
    interview_memo = efficient_llm.invoke(
        [SystemMessage(content=prompts.write_memo_system)] + [HumanMessage(content=human_msg)]
    )

    # Write messages to state
    return {'interview_memos': [interview_memo.content]}


# Industry Research SubGraph
builder = StateGraph(IndustryInterviewState)
builder.add_node(interview_question)
builder.add_node(web_search)
builder.add_node(generate_answer)
builder.add_node(write_interview_memo)
builder.add_edge(START, 'interview_question')
builder.add_edge('interview_question', 'web_search')
builder.add_edge('web_search', 'generate_answer')
builder.add_conditional_edges('generate_answer', router_after_answer)
builder.add_edge('write_interview_memo', END)
industry_research_subgraph = builder.compile()

# endregion

# region === Part III Academic Research SubGraph ===

### === ArXiv Search ===
# Lock for Arxiv: Ensures strictly one request happens at a time
GLOBAL_ARXIV_LOCK = asyncio.Lock()

# Node for ArXiv Search
async def arxiv_search(state: AcademicResearchState):
    """
    Async wrapper for ArXiv search.
    """
    query = state['query']
    arxiv_papers = []

    async with GLOBAL_ARXIV_LOCK:
        try:
            arxiv_papers = await asyncio.to_thread(utils.run_sync_arxiv_search, query)
        
        except Exception as e:
            logger.error(f"ArXiv search failed: {e}")

        await asyncio.sleep(3.0)
        
    # Zero Result Check
    count = len(arxiv_papers)
    if count == 0:
        logger.warning(f"⚠️ NO PAPERS FOUND from ArXiv for query: '{query}'.")

    return {'arxiv_papers': sorted(arxiv_papers, key=lambda x: x['published_date'], reverse=False)}


# Node to calculate relevance score
def relevance_score(state: AcademicResearchState):
    """Calculates relevance score for the papers."""
    # Get state
    topic = state['topic']
    papers = state['arxiv_papers']
    
    if not papers:
        return {'arxiv_papers': papers}

    # Deduplicate papers, keep first(higher arxiv.SortCriterion.Relevance) 
    seen_ids = set()
    unique_papers = []
    for paper in papers:
        if paper['arxiv_id'] not in seen_ids:
            unique_papers.append(paper)
            seen_ids.add(paper['arxiv_id'])
    
    initial_count = len(unique_papers)
            
    # Extract paper texts
    paper_texts = [f"{p['title']}. {p['abstract']}" for p in unique_papers]

    # Compute embeddings
    paper_embeddings = embedding_model.encode(
        paper_texts, 
        batch_size=32,
        convert_to_numpy=True,
        show_progress_bar=False,
        normalize_embeddings=True
    )

    topic_embedding = embedding_model.encode(
        topic, 
        convert_to_numpy=True, 
        normalize_embeddings=True
    ).reshape(-1, 1)

    # use dot product to compute cosine similarity as relevance score
    relevance = np.dot(paper_embeddings, topic_embedding).flatten()

    relevant_papers = []
    for i, score in enumerate(relevance):
        if score >= 0.15:
            paper = unique_papers[i]
            paper['embedding'] = paper_embeddings[i]
            paper['relevance_score'] = float(score)
            relevant_papers.append(paper)

    # Logging the number of low relevance papers
    dropped_count = initial_count - len(relevant_papers)
    if dropped_count > 0:
        percent_dropped = (dropped_count / initial_count) * 100
        logger.info(f"Relevance Filter: Dropped {percent_dropped:.2f}% / {dropped_count} papers (relevance < 0.15)")

    return {'arxiv_papers': relevant_papers}


### === Semantic Scholar Metrics Extraction ===
# Semantic Scholar API key
S2_API_KEY = os.getenv('S2_API_KEY')

# Semaphore for S2: Allows 5 concurrent HTTP requests across all subgraphs
_S2_SEMAPHORE = None

def get_s2_semaphore() -> asyncio.Semaphore:
    global _S2_SEMAPHORE
    if _S2_SEMAPHORE is None:
        _S2_SEMAPHORE = asyncio.Semaphore(5)
    return _S2_SEMAPHORE

# Node to extract citations and influential citations from Semantic Scholar
async def semantic_scholar_metrics(state: AcademicResearchState):
    """
    Extract citations, influential citations and author h-index from Semantic Scholar.
    """
    # Get state
    papers = state['arxiv_papers']   

    if not papers:
        return {'arxiv_papers': papers}

    # Get list of arxiv ids
    papers_df = pd.DataFrame(papers)   
    arxiv_ids = papers_df['arxiv_id'].tolist() 

    # Set up headers with API key if available
    headers = {"x-api-key": S2_API_KEY} if S2_API_KEY else {}

    # Grab the shared global semaphore for S2 API calls
    S2_SEMAPHORE = get_s2_semaphore()

    # Fetch Semantic Scholar metrics
    async with httpx.AsyncClient(timeout=30.0) as client:
        s2_metrics_df = await utils.fetch_s2_metrics(
            arxiv_ids=arxiv_ids, 
            client=client,
            semaphore=S2_SEMAPHORE, 
            headers=headers,
            max_retries=3,    
        )

    # Log percentage of papers not found in Semantic Scholar    
    total_papers = len(papers)
    s2_papers = len(s2_metrics_df)
    if total_papers > s2_papers:
        percent_not_found = (total_papers - s2_papers) / total_papers * 100
        logger.info(f"{percent_not_found:.2f}% of papers not found in Semantic Scholar.")

    # Columns to add
    cols = ['citations', 'influential_citations', 'author_score']
    
    # Merge the two dataframes
    if not s2_metrics_df.empty:
        papers_df = pd.merge(papers_df, s2_metrics_df, on='arxiv_id', how='left')
    else:
        for col in cols:
            papers_df[col] = 0
        
    # Fill NaN (papers not found in S2) with 0
    papers_df['citations'] = papers_df['citations'].fillna(0).astype(int)
    papers_df['influential_citations'] = papers_df['influential_citations'].fillna(0).astype(int)
    papers_df['author_score'] = papers_df['author_score'].fillna(0.0).astype(float)
    
    return {'arxiv_papers': papers_df.to_dict(orient="records")}


### === HuggingFace Metrics Extraction ===
# HF Token
HF_TOKEN = os.getenv('HF_TOKEN')

# Semaphore for HF
_HF_PAPER_SEMAPHORE = None
_HF_MODEL_SEMAPHORE = None

def get_hf_semaphores() -> tuple[asyncio.Semaphore, asyncio.Semaphore]:
    global _HF_PAPER_SEMAPHORE, _HF_MODEL_SEMAPHORE
    if _HF_PAPER_SEMAPHORE is None:
        _HF_PAPER_SEMAPHORE = asyncio.Semaphore(10)
    if _HF_MODEL_SEMAPHORE is None:
        _HF_MODEL_SEMAPHORE = asyncio.Semaphore(2)
    return _HF_PAPER_SEMAPHORE, _HF_MODEL_SEMAPHORE

# Node to extract upvotes, githubStars, and model citations from huggingface
async def huggingface_metrics(state: AcademicResearchState):
    """
    Extracts upvotes, githubStars, and model citations from HuggingFace.
    """
    # Get state
    papers = state['arxiv_papers']   

    if not papers:
        return {'arxiv_papers': papers}

    # Get list of arxiv ids
    papers_df = pd.DataFrame(papers)   
    arxiv_ids = papers_df['arxiv_id'].tolist() 

    # Set up headers with HF token if available
    headers = {"Authorization": f"Bearer {HF_TOKEN}"} if HF_TOKEN else {}

    # Grab the shared global semaphores for HF API calls
    HF_PAPER_SEMAPHORE, HF_MODEL_SEMAPHORE = get_hf_semaphores()

    # Setup async HTTP client
    async with httpx.AsyncClient(timeout=30.0) as client:
        hf_metrics_df = await utils.fetch_hf_metrics(
            arxiv_ids=arxiv_ids,
            client=client,
            paper_semaphore = HF_PAPER_SEMAPHORE,
            model_semaphore = HF_MODEL_SEMAPHORE,
            headers=headers,
            max_retries=3,
        ) 

    # Columns to add
    cols = ['hf_upvotes', 'github_stars', 'hf_model_references']

    if not hf_metrics_df.empty:
        # Log percentage of papers not found in Huggingface,
        percent_not_found = (hf_metrics_df['paper_not_found'] == 1).mean() * 100
        logger.info(f"{percent_not_found:.2f}% of papers not found in HuggingFace.")

        # Clean hf_metrics_df
        hf_metrics_df = hf_metrics_df.drop(columns=['paper_not_found'])
             
        # Merge the two dataframes  
        papers_df = pd.merge(papers_df, hf_metrics_df, on='arxiv_id', how='left')

        # Fill NaN with 0
        for col in cols:
            papers_df[col] = papers_df[col].fillna(0).astype(int)
    
    else:
        for col in cols:
            papers_df[col] = 0
        logger.info("100% of papers not found in HuggingFace.")
    
    return {'arxiv_papers': papers_df.to_dict(orient="records")}


# Node to split the papers into three buckets base on their published date
def temporal_stratification(state: AcademicResearchState):
    """
    Split the papers into three buckets base on their published date.
    """
    # Get state
    papers = state['arxiv_papers']   

    if not papers:
        return {'arxiv_papers': papers, 'impact_papers': [], 
                'momentum_papers': [], 'latest_papers': []}

    # Setup dates
    current = datetime.now(timezone.utc)
    day_90_cutoff = current - timedelta(days=90)
    day_30_cutoff = current - timedelta(days=30)

    impact_papers = []
    momentum_papers = []
    latest_papers = []

    # Distribute papers into three buckets based on their age
    for p in papers:
        pub_date = p['published_date']

        if isinstance(pub_date, str):
            pub_date = datetime.fromisoformat(pub_date.replace('Z', '+00:00'))
            p['published_date'] = pub_date
        
        if pub_date.tzinfo is None:
            pub_date = pub_date.replace(tzinfo=timezone.utc)

        if pub_date < day_90_cutoff:
            impact_papers.append(p)
        elif pub_date > day_30_cutoff:
            latest_papers.append(p)
        else:
            momentum_papers.append(p)

    return {'impact_papers': impact_papers, 'momentum_papers': momentum_papers, 
            'latest_papers': latest_papers, 'arxiv_papers': []}


# Node to rank impact papers
def rank_impact_papers(state: AcademicResearchState):
    """Rank impact papers."""
    # Get state
    impact_papers = state['impact_papers']   

    if not impact_papers:
        return {'impact_papers': impact_papers}

    impact_df = pd.DataFrame(impact_papers)

    # Calculate monthly metrics
    impact_df['published_date'] = pd.to_datetime(impact_df['published_date'], utc=True)
    current_date = pd.Timestamp.now('UTC')
    diff = current_date - impact_df['published_date']
    impact_df['months'] = round(diff.dt.days / 30, 1)

    metrics = ['citations', 'influential_citations', 'hf_upvotes', 'github_stars', 'hf_model_references']
    for metric in metrics:
        impact_df[f'{metric}_stability'] = impact_df[metric] / pow(impact_df['months']+1, 0.8)
    
    # Calculate final score
    impact_df = utils.calculate_final_score(impact_df, 'impact')

    return {'impact_papers': impact_df.to_dict(orient="records")}


# Node to calculate novelty_score
def novelty_score(state: AcademicResearchState):
    """
    Calculates novelty scores:
    - Latest papers: 1 - max_similarity(momentum_papers)
    - Momentum papers: 1 - similarity(momentum_centroid)
    """
    # Get state
    momentum_papers = state['momentum_papers']
    latest_papers = state['latest_papers']

    if not momentum_papers:
        return {'momentum_papers': momentum_papers, 'latest_papers': latest_papers}

    # Prepare momentum embeddings matrix
    momentum_embedding = np.vstack([p['embedding'] for p in momentum_papers])

    # Process latest papers: calculate max similarity against any momentum paper
    if latest_papers:
        latest_embedding = np.vstack([p['embedding'] for p in latest_papers])
        latest_sim = np.dot(latest_embedding, momentum_embedding.T)
        max_sim = np.max(latest_sim, axis=1)
        novelty_score = (1 - max_sim).tolist()
        for i, paper in enumerate(latest_papers):
            paper['novelty_score'] = novelty_score[i]

    # Process momentum papers: calculate similarity to centroid
    centroid = np.mean(momentum_embedding, axis=0).reshape(-1, 1)
    momentum_sim = np.dot(momentum_embedding, centroid)
    momentum_novelty = (1 - momentum_sim.flatten()).tolist()
    for i, paper in enumerate(momentum_papers):
        paper['novelty_score'] = momentum_novelty[i]

    return {'momentum_papers': momentum_papers, 'latest_papers': latest_papers}


# Node to calculate concept_match
def concept_match(state: AcademicResearchState):
    """
    Calculates concept_match against validated Impact anchors.
    Score = Max similarity to any single paper in the Top 50 validated Impact set.
    """
    # Get state
    impact_papers = state['impact_papers']
    momentum_papers = state['momentum_papers']
    latest_papers = state['latest_papers']

    if not impact_papers:
        return {'momentum_papers': momentum_papers, 'latest_papers': latest_papers}

    # Create anchor set(impact_papers already sorted by final_score in rank_impact_papers node)
    anchors = [
        p for p in impact_papers
        if (p['citations'] >= 5) or (p['influential_citations'] > 0)
    ][:50]

    if not anchors:
        anchors = impact_papers[:5]

    # Prepare anchor embeddings matrix
    anchor_embedding = np.vstack([p['embedding'] for p in anchors])

    # Process latest papers
    if latest_papers:
        latest_embedding = np.vstack([p['embedding'] for p in latest_papers])
        latest_sim = np.dot(latest_embedding, anchor_embedding.T)
        max_sim = np.max(latest_sim, axis=1).tolist()
        for i, paper in enumerate(latest_papers):
            paper['concept_match'] = max_sim[i]

    # Process momentum papers
    if momentum_papers:
        momentum_embedding = np.vstack([p['embedding'] for p in momentum_papers])
        momentum_sim = np.dot(momentum_embedding, anchor_embedding.T)
        max_sim = np.max(momentum_sim, axis=1).tolist()
        for i, paper in enumerate(momentum_papers):
            paper['concept_match'] = max_sim[i]

    return {'momentum_papers': momentum_papers, 'latest_papers': latest_papers}


# Node to rank momentum and latest papers
def rank_momentum_latest(state: AcademicResearchState):
    """Rank momentum and latest papers."""
    # Get state
    momentum_papers = state['momentum_papers']   
    latest_papers = state['latest_papers']

    if momentum_papers:
        momentum_df = pd.DataFrame(momentum_papers)
        momentum_df = utils.calculate_velocity(momentum_df)
        momentum_df = utils.calculate_final_score(momentum_df, 'momentum')
        momentum_papers = momentum_df.to_dict(orient="records")
    
    if latest_papers:
        latest_df = pd.DataFrame(latest_papers)
        latest_df = utils.calculate_velocity(latest_df)
        latest_df = utils.calculate_final_score(latest_df, 'latest')
        latest_papers = latest_df.to_dict(orient="records")
    
    return {'momentum_papers': momentum_papers, 'latest_papers': latest_papers}


# Node to write research summary
def write_research_summary(state: AcademicResearchState):
    """
    Write a research summary based on the provided papers.
    """
    # Get state
    topic = state['topic']
    query = state['query']
    impact_papers = state['impact_papers']
    momentum_papers = state['momentum_papers']
    latest_papers = state['latest_papers']

    if not (impact_papers or momentum_papers or latest_papers):
        return {'research_summaries': []}

    impact_df = pd.DataFrame(impact_papers)
    momentum_df = pd.DataFrame(momentum_papers)
    latest_df = pd.DataFrame(latest_papers)

    # Get abstracts of top-ranked papers
    impact_paper_abstracts = "\n\n".join([
        f"<Document source='{paper['url']}'>\n"
        f"PublishedDate: {paper['published_date']}\n"
        f"Title: {paper['title']}\n"
        f"Abstract: {paper['abstract']}\n"
        f"</Document>"
        for paper in impact_df.head(10).to_dict(orient='records')
    ])

    momentum_paper_abstracts = "\n\n".join([
        f"<Document source='{paper['url']}'>\n"
        f"PublishedDate: {paper['published_date']}\n"
        f"Title: {paper['title']}\n"
        f"Abstract: {paper['abstract']}\n"
        f"</Document>"
        for paper in momentum_df.head(10).to_dict(orient='records')
    ])

    latest_paper_abstracts = "\n\n".join([
        f"<Document source='{paper['url']}'>\n"
        f"PublishedDate: {paper['published_date']}\n"
        f"Title: {paper['title']}\n"
        f"Abstract: {paper['abstract']}\n"
        f"</Document>"
        for paper in latest_df.head(5).to_dict(orient='records')
    ])

    # Human message
    human_msg = prompts.research_summary_human.format(
        topic=topic,
        query=query,
        impact_paper_abstracts=impact_paper_abstracts,
        momentum_paper_abstracts=momentum_paper_abstracts,
        latest_paper_abstracts=latest_paper_abstracts,
    )

    response = efficient_llm.invoke(
        [SystemMessage(content=prompts.research_summary_system)] + [HumanMessage(content=human_msg)]
    )

    return {'research_summaries': [response.content]}


# Academic research subgraph
builder = StateGraph(state_schema=AcademicResearchState, output_schema=AcademicResearchOutput)
builder.add_node("arxiv_search", arxiv_search)
builder.add_node("relevance_score", relevance_score)
builder.add_node("semantic_scholar_metrics", semantic_scholar_metrics)
builder.add_node("huggingface_metrics", huggingface_metrics)
builder.add_node("temporal_stratification", temporal_stratification)
builder.add_node("rank_impact_papers", rank_impact_papers)

builder.add_node("novelty_score", novelty_score)
builder.add_node("concept_match", concept_match)
builder.add_node("rank_momentum&latest", rank_momentum_latest)
builder.add_node("write_research_summary", write_research_summary)

builder.add_edge(START, 'arxiv_search')
builder.add_edge('arxiv_search', 'relevance_score')
builder.add_edge('relevance_score', 'semantic_scholar_metrics') 
builder.add_edge('semantic_scholar_metrics', 'huggingface_metrics')
builder.add_edge('huggingface_metrics', 'temporal_stratification')
builder.add_edge('temporal_stratification', 'rank_impact_papers')
builder.add_edge('temporal_stratification', 'novelty_score')
builder.add_edge('novelty_score', 'concept_match')
builder.add_edge('concept_match', 'rank_momentum&latest')
builder.add_edge('rank_impact_papers', "write_research_summary")
builder.add_edge('rank_momentum&latest', "write_research_summary")
builder.add_edge("write_research_summary", END)

academic_research_subgraph = builder.compile()

# endregion

# region == Part IV Final Tech Blog ==

# Node to write technical blog
def write_tech_blog(state: TrendTrackerState):
    """Write technical blog."""
    # Get state
    topic = state['topic']
    interview_memos = state['interview_memos']
    research_summaries = state['research_summaries']
    tech_blog = state.get('tech_blog') or "N/A"
    blog_critique = state.get('blog_critique') or "N/A"

    # Convert interview memos and research summaries to long strings
    industry_research = "\n\n".join([m for m in interview_memos])
    academic_research = "\n\n".join([s for s in research_summaries])

    # Human message
    human_msg = prompts.tech_blog_human.format(
        topic=topic,
        industry_research=industry_research,
        academic_research=academic_research,
        tech_blog=tech_blog,
        blog_critique=blog_critique,
    )

    response = frontier_llm.invoke(
        [SystemMessage(content=prompts.tech_blog_system)] + [HumanMessage(content=human_msg)]
    )

    return {'tech_blog': response.content, 'blogger_retry_count': 1}


# Node to review the blog
def review_tech_blog(state: TrendTrackerState):
    """Review technical blog."""
    # Get state
    tech_blog = state['tech_blog']
    interview_memos = state['interview_memos']
    research_summaries = state['research_summaries']
    blog_critique = state.get('blog_critique') or "N/A"
    retry = state['blogger_retry_count']

    if retry >= 3:
        return {'blog_critique': state['blog_critique']}

    # Convert interview memos and research summaries to long strings
    industry_research = "\n\n".join([m for m in interview_memos])
    academic_research = "\n\n".join([s for s in research_summaries])

    # Human message
    human_msg =  prompts.critique_human.format(
        tech_blog=tech_blog,
        industry_research=industry_research,
        academic_research=academic_research,
        blog_critique=blog_critique
    )

    response = frontier_llm.invoke(
        [SystemMessage(content=prompts.critique_system)] + [HumanMessage(content=human_msg)]
    )

    return {'blog_critique': response.content}


# Conditional routing logic after review_tech_blog node
def critique_router(state: TrendTrackerState) -> Literal['write_tech_blog', END]:
    """Conditional routing logic after critique"""
    # Get state
    blog_critique = state['blog_critique']
    retry = state['blogger_retry_count']

    if 'Good job' in blog_critique or retry >= 3:
        return END
    else:
        return 'write_tech_blog'


# Build the Trend Tracker Agent Graph
agent_builder = StateGraph(TrendTrackerState)
agent_builder.add_node('research_planner', research_planner)
agent_builder.add_node('plan_router', plan_router)
agent_builder.add_node('plan_reviewer', plan_reviewer)
agent_builder.add_node('review_router', review_router)
agent_builder.add_node('create_analysts', create_analysts)
agent_builder.add_node('create_arxiv_query', create_arxiv_query)
agent_builder.add_node('dispatch_research', dispatch_research)
agent_builder.add_node('industry_research_subgraph', industry_research_subgraph)
agent_builder.add_node('academic_research_subgraph', academic_research_subgraph)
agent_builder.add_node('write_tech_blog', write_tech_blog)
agent_builder.add_node('review_tech_blog', review_tech_blog)
agent_builder.add_edge(START, 'research_planner')
agent_builder.add_edge('research_planner', 'plan_router')
agent_builder.add_edge('plan_reviewer', 'review_router')
agent_builder.add_edge('create_analysts', 'dispatch_research')
agent_builder.add_edge('create_arxiv_query', 'dispatch_research')
agent_builder.add_conditional_edges('dispatch_research', dispatch_router, ['industry_research_subgraph', 'academic_research_subgraph'])
agent_builder.add_edge('industry_research_subgraph', 'write_tech_blog')
agent_builder.add_edge('academic_research_subgraph', 'write_tech_blog')
agent_builder.add_edge('write_tech_blog', 'review_tech_blog')
agent_builder.add_conditional_edges('review_tech_blog', critique_router)
trend_tracker_agent = agent_builder.compile()

# endregion