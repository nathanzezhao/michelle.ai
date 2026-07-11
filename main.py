import os
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from llm import ask_llm

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class UserMessage(BaseModel):
    text: str
    
@app.post("/chat")
def handle_chat(incoming_data: UserMessage):
    provider = os.getenv("LLM_PROVIDER", "mock")
    print(f"[{provider}] User said: {incoming_data.text}")

    try:
        answer = ask_llm(incoming_data.text)
        print(f"[{provider}] Michelle responded: {answer}")
        return {"answer": answer}
    except Exception as e:
        print(f"Error: {e}")
        return {
            "answer": "Sorry, I am having some trouble with this right now. Please try again later."
        }
