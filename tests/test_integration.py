import pytest
import uuid
import os
from langgraph.types import Command
from langgraph.checkpoint.memory import InMemorySaver
from src.agent import agent_builder  # Local Import

# Setup a fixture for the agent
@pytest.fixture
def agent():
    """Compiles the agent graph once for the test session."""
    return agent_builder.compile(checkpointer=InMemorySaver())

# Run an Async Test to Simulate the Agent's Workflow
@pytest.mark.asyncio
async def test_trend_tracker_flow(agent):
    """
    This test simulates the entire workflow of the agent, including handling interrupts
    and producing output. It uses assertions to validate that the expected data is generated.
        
    """
    print(
        f"\n{'=' * 75}\n"
        "Starting test for Trend Tracker Agent...\n"
        "This test orchestrates a complex LangGraph architecture for a deep research.\n"
        "Expected execution time: 12-18 minutes.\n"
        "Grab a coffee :)\n"
        f"{'=' * 75}\n"
    )

    config = {'configurable': {'thread_id': str(uuid.uuid4())}}
    test_topic = 'Track the trend of parameter-efficient fine-tuning (PEFT) for LLMs in the last 12 months.'
    
    # Run the agent
    results = await agent.ainvoke({'topic': test_topic}, config=config)

    # Handling Interrupts (Human-in-the-loop) automatically by simulating 'approve' feedback
    while '__interrupt__' in results:
        results = await agent.ainvoke(Command(resume="approve"), config=config)

    # Assertions to Validate Output
    assert results is not None
    assert 'topic' in results
    assert results['topic'] == test_topic

    # Check if interview_memos were generated
    assert 'interview_memos' in results
    assert isinstance(results['interview_memos'], list)
    assert len(results['interview_memos']) > 0

    # Check if research_summaries was generated
    assert 'research_summaries' in results
    assert isinstance(results['research_summaries'], list)
    assert len(results['research_summaries']) > 0

    # Check if a blog was generated
    assert 'tech_blog' in results
    assert len(results['tech_blog']) > 0

    # Check if the blog content contains expected keywords
    assert 'parameter-efficient fine-tuning' in results['tech_blog'].lower() or 'peft' in results['tech_blog'].lower()