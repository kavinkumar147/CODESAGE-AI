# CodeSage AI — Stage 1: Core Review Pipeline

This is Stage 1 of the AI Code Review & Bug Detection Agent: the core
pipeline (static analysis → LLM reasoning → Markdown formatting), fully
testable locally with **no GitHub or deployment involved yet**.

## What's included

```
app/
  config.py            # env var loading
  static_analysis.py   # Pylint / Bandit / ESLint runners
  llm_review.py         # Claude API call + strict JSON parsing
  formatter.py           # JSON findings -> Markdown PR comment
  pipeline.py             # ties it all together
tests/
  fixtures/
    sample_pr.diff       # sample diff with intentional bugs
    user_service.py       # full file content matching the diff
  run_local_review.py     # manual smoke test — run this first
  test_formatter.py        # unit tests, no API key needed
  test_llm_review.py        # unit tests, no API key needed
requirements.txt
.env.example
```

## Setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. For JS/TS linting support, ESLint needs to be available via `npx`.
   If you're only testing with the Python fixture, you can skip this for now:

   ```bash
   npm install eslint --save-dev
   ```

3. Copy the env template and add your Anthropic API key:

   ```bash
   cp .env.example .env
   # then edit .env and set ANTHROPIC_API_KEY
   ```

## Run the smoke test

This runs the full pipeline against a sample diff containing an intentional
SQL injection, a hardcoded password, and a couple of logic issues — a good
way to confirm the LLM reasoning + static analysis + formatting are all
working together before wiring up GitHub.

```bash
python -m tests.run_local_review
```

You should see:
- Pylint/Bandit findings printed (static analysis working)
- A structured summary + findings list (LLM reasoning working)
- A rendered Markdown comment at the bottom (formatting working) —
  it should flag the SQL injection and hardcoded password as **Critical**.

## Run unit tests

These don't require an API key or network access — they test JSON parsing
and Markdown formatting in isolation:

```bash
pytest tests/test_formatter.py tests/test_llm_review.py -v
```

## What "done" looks like for Stage 1

- [ ] `python -m tests.run_local_review` runs without errors
- [ ] The rendered comment correctly flags the SQL injection as Critical
- [ ] The rendered comment correctly flags the hardcoded password as Critical
- [ ] `pytest` unit tests pass
- [ ] You've read through `app/llm_review.py`'s `SYSTEM_PROMPT` and tweaked
      the rubric if you want a different review style/tone

## Next: Stage 2

Once Stage 1 works locally, Stage 2 adds the FastAPI webhook server, GitHub
App authentication, diff fetching, and comment posting — turning this into
a real bot that reacts to PRs. That will use the `GITHUB_APP_ID`,
`GITHUB_PRIVATE_KEY`, and `GITHUB_WEBHOOK_SECRET` values you already have
from setting up the GitHub App.
