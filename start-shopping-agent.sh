nohup .venv/bin/uvicorn shopping_agent.main:app --reload --host 0.0.0.0 --port 8000 --ssl-certfile localhost+1.pem --ssl-keyfile localhost+1-key.pem > logs/shopping-agent-stdouterr.log 2>&1 &
