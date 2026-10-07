# Python bot + a tiny Node helper that builds STON.fi swap messages with the official SDK.
FROM nikolaik/python-nodejs:python3.11-nodejs20-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot/swap_builder/package.json bot/swap_builder/package.json
RUN cd bot/swap_builder && npm install --omit=dev
COPY . .
CMD ["sh", "-c", "cd bot && python main.py"]
