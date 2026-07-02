import os
from google import genai



from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app=FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  
    allow_credentials=True,
    allow_methods=["*"],  
    allow_headers=["*"],
)

class UserMessage(BaseModel):
    text: str

import os
from dotenv import load_dotenv   
load_dotenv()

os.environ["GEMINI_API_KEY"] = os.getenv("GEMINI_API_KEY")
client = genai.Client()

@app.post("/chat")

def HandleChat(incoming_data: UserMessage):
    print(f"User said: {incoming_data.text}")
    try:
        response=client.models.generate_content(
            model='gemini-3.5-flash',
            contents=incoming_data.text,
        )

        print(f"Gemini responded: {response.text}")

        return {"answer": response.text}
    
    except Exception as e:
        print(f"Error: {e}")
        return{"answer": "Sorry, I am having some trouble with this right now. Please try again later."}
    


  





