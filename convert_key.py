from pathlib import Path

pem = Path("private-key.pem").read_text()

print(pem.replace("\n", "\\n"))