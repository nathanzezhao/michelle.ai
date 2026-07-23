Uses Electron to create a desktop window hosting the AI agent michelle.ai.

Project roadmap and checklist: [ROADMAP.md](ROADMAP.md)

VIDEO DEMOS:

Video demo 07/02/2026:

[https://github.com/user-attachments/assets/36f5f3f5-7986-40b3-92b2-21973c9dfdfb](https://github.com/user-attachments/assets/36f5f3f5-7986-40b3-92b2-21973c9dfdfb)

Video demo 07/17/2026 (memory.py, intent.py, llm.py, michelle.db):

https://github.com/user-attachments/assets/201dd571-df6e-40d5-bfd9-fc0a9b7425e9

LLMs:

- Ollama
    - Local backend AI with no memory, start by running “python3 -m uvicorn main:app —reload” in terminal
    - Should be default AI in use,  if not find .env file and set LLM_PROVIDER to ollama and run “ollama pull llama3.2” in terminal
    - Refresh backend and electron by pressing control + c.
- Mock
    - Very basic local mock AI for simple responses.
    - Set LLM_PROVIDER=mock to use
    - Refresh backend and electron by pressing control + c.
- Gemini
    - Paid with tokens.
    - set .env file LLM_PROVIDER=gemini
    - Refresh backend and electron by pressing control + c.

UPDATES:

As of July 15 2026, Michelle has a memory of the last 10 messages in current chat session. That means that if you state your name in the first message and chat about other things for another 10 messages, asking her for your name on the 11th will result in her not remembering. 

`michelle.db` is the filing cabinet.  It stores every chat from every session. `memory.py` opens that cabinet and pulls out the last 10 messages. `main.py` sends those plus your new message to the LLM. So the DB remembers everything, but Michelle only "sees" the most recent 10 messages each time.

main.py  →  memory.py (get last 10 from DB)
→  llm.py (send those + new message to Gemini/Ollama/mock)
→  memory.py (save the new turn)

Michelle now also has intent routers behind every message before being sent to an llm in the form of either an estimated or hardcoded number between 0 an 1.0 to distinguish between three different main intent sections:

- Chat -> Small talk, greetings, casual conversation → Ollama generates reply
- Action -> User wants Michelle to do something → stub reply (not built yet)
- Retrieve -> User wants info from docs/data → stub reply (not built yet)

.env setup for intent:

INTENT_MODE=llm      ← default, uses Gemini
INTENT_MODE=rules    ← keyword matching, no API
INTENT_MODE=mock     ← simplest keywords, for terminal testing

If you choose to use ollama as the llm, the intent router is a set of hardcoded parameters that are tested against what you type when chatting. 
