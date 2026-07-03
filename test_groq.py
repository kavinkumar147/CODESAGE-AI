from dotenv import load_dotenv
from openai import OpenAI
import os

load_dotenv()  # Load variables from .env

client = OpenAI(
    api_key=os.getenv("GROQ_API_KEY"),
    base_url="https://api.groq.com/openai/v1",
)

response = client.chat.completions.create(
    model="llama-3.3-70b-versatile",
    messages=[
        {"role": "user", "content": "Reply with only the word: Hello"}
    ],
    max_tokens=10,
)

print(response.choices[0].message.content)