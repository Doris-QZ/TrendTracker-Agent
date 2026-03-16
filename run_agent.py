import asyncio
import uuid
import time
import json
from pprint import pprint
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from src.agent import agent_builder    # Local Import


async def main():
    app = agent_builder.compile(checkpointer=InMemorySaver())
    config = {'configurable': {'thread_id': str(uuid.uuid4())}}

    print(
        "\nFor best results, provide a specific and focused research topic related to AI/ML trends with a clear timeframe.\n"
        "Examples:\n"
        " - Track the trend of parameter-efficient fine-tuning (PEFT) for LLMs in the last 12 months.\n"
        " - Track the trend of Vision-Language-Action (VLA) models for robotic manipulation in the last 24 months.\n"
        " - Track the trend of Retrieval-Augmented Generation (RAG) adoption in the healthcare industry over the past year.\n",
        flush=True 
    )

    topic = input(f"What topic would you like to track the trend in: ") 

    start = time.monotonic()
    results = await app.ainvoke({'topic': topic}, config=config)

    while '__interrupt__' in results:
        pprint((results['__interrupt__'][-1].value))
        feedback = input("Provide your feedback (or type 'approve' to continue): ")
        results = await app.ainvoke(Command(resume=feedback), config=config)
    
    if 'tech_blog' in results:
        with open("tech_blog.md", "w", encoding="utf-8") as f:
            f.write(results['tech_blog'])
            print("Saved blog to 'tech_blog.md'.")
    else:
        with open("results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=4, default=str)
            print("Saved results to 'results.json'.")
    
    end = time.monotonic()
    total_seconds = int(end - start)
    minutes, seconds = divmod(total_seconds, 60)
    print(f"Total execution time: {int(minutes)} minutes and {int(seconds)} seconds.")

if __name__ == "__main__":
    asyncio.run(main())