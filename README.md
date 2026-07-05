CODESAGE-AI:

> AI-Powered GitHub Pull Request Review Assistant

Automatically reviews GitHub Pull Requests using Static Analysis + AI reasoning and posts intelligent review comments directly on GitHub while providing a modern real-time dashboard.

---

# 🚀 Overview

CodeSage AI is an AI-powered GitHub Pull Request Review Assistant designed to automate the first stage of code reviews.

Whenever a developer opens or updates a Pull Request, CodeSage AI automatically:

- Detects the Pull Request using GitHub Webhooks
- Downloads the changed files
- Performs Static Code Analysis
- Sends the code changes to an AI model (Groq Llama)
- Generates human-like review suggestions
- Posts the review back to GitHub
- Updates a live dashboard showing the latest review

This reduces manual review effort and helps developers identify bugs, code smells, security issues, and improvement suggestions before human reviewers begin reviewing the code.

---

# ✨ Features

## GitHub Integration

- GitHub App Authentication
- Secure Webhook Signature Verification
- Pull Request Event Handling
- Automatic PR Comment Posting

---

## AI Code Review

- AI-powered review using Groq Llama
- Human-like review suggestions
- Bug Detection
- Security Analysis
- Code Quality Improvements
- Best Practice Recommendations

---

## Static Analysis

- Pylint
- Bandit
- ESLint
- Automatic Severity Classification

---

## Dashboard

- Modern Responsive UI
- Latest Pull Request Information
- Review Status
- Files Reviewed
- Static Analysis Findings
- AI Review Summary
- Live Backend Status

---

# 🏗 Project Architecture

```

Developer
│
▼
GitHub Pull Request

│

▼
GitHub Webhook

│

▼
FastAPI Backend

├── Verify Signature
├── GitHub App Authentication
├── Download Changed Files
├── Static Analysis
├── AI Review (Groq)
├── Format Review
├── Post GitHub Comment
└── Update Dashboard

│

▼

Dashboard + GitHub Comment

```

---

# 📁 Project Structure

```

CodeSage-AI/
│
├── app/
│ ├── main.py
│ ├── github_client.py
│ ├── pipeline.py
│ ├── static_analysis.py
│ ├── llm_review.py
│ ├── formatter.py
│ ├── state.py
│ └── config.py
│
├── frontend/
│ ├── index.html
│ ├── style.css
│ └── script.js
│
├── tests/
│
├── requirements.txt
├── README.md
├── .env.example
└── test_groq.py

```

---

# ⚙ Tech Stack

## Backend

- Python
- FastAPI
- Uvicorn

## AI

- Groq API
- Llama Model

## Static Analysis

- Pylint
- Bandit
- ESLint

## GitHub

- GitHub Apps
- GitHub Webhooks
- GitHub REST API

## Frontend

- HTML
- CSS
- JavaScript

## Deployment

- Render

---

# 🔄 Workflow

1. Developer creates a Pull Request.

2. GitHub sends a Webhook.

3. FastAPI receives the request.

4. Webhook signature is verified.

5. GitHub App generates an Installation Token.

6. Changed files are downloaded.

7. Static Analysis runs.

8. AI reviews the code.

9. Markdown review is generated.

10. Review is posted back to GitHub.

11. Dashboard updates automatically.

---

# 🚀 Installation

## Clone the Repository

```bash
git clone https://github.com/<your-username>/CODESAGE-AI.git

cd CODESAGE-AI
```

## Create Virtual Environment

```bash
python -m venv .venv
```

Windows

```bash
.venv\Scripts\activate
```

Linux / macOS

```bash
source .venv/bin/activate
```

---

## Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configure Environment Variables

Create a `.env` file.

Example:

```text
GITHUB_APP_ID=

GITHUB_PRIVATE_KEY=

GITHUB_WEBHOOK_SECRET=

GROQ_API_KEY=
```

---

## Run the Backend

```bash
uvicorn app.main:app --reload
```

---

## Open Dashboard

```
http://127.0.0.1:8000/dashboard/
```

---

# 📡 API Endpoints

| Method | Endpoint | Description |
|---------|----------|-------------|
| GET | /health | Health Check |
| POST | /webhook | GitHub Webhook |
| GET | /api/latest-review | Latest Review Data |
| GET | /dashboard/ | Dashboard |

---

# 📸 Demo

## Dashboard

<img width="1920" height="1080" alt="Screenshot 2026-07-04 213224" src="https://github.com/user-attachments/assets/b1b32738-653d-443e-9f73-dc4b49e6e91c" />

---

## AI Review Comment

<img width="962" height="1072" alt="Screenshot 2026-07-03 234607" src="https://github.com/user-attachments/assets/c854d2bd-46f5-4e7e-82a1-175171383b9d" />



# ✅ Example Workflow

Developer pushes code

↓

Creates Pull Request

↓

GitHub sends Webhook

↓

CodeSage AI reviews the code

↓

Review is posted automatically

↓

Dashboard displays latest review

---

# 🚀 Future Improvements

- Multi-Repository Support
- Team Dashboard
- Slack Integration
- Microsoft Teams Integration
- Email Notifications
- Historical Analytics
- Auto Review Approval
- Multi-LLM Support

---

Author:

**KAVINKUMAR G**

AI & Data Science Engineer

GitHub: https://github.com/kavinkumar147

#Thankyou All.
docker test agin 
