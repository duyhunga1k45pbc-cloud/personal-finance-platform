# Personal Finance API

A simple backend API for personal finance management built with FastAPI, PostgreSQL, SQLAlchemy, and Pydantic.

## Features

- Create transaction
- Get transaction list
- Get transaction detail
- Update transaction
- Delete transaction
- Filter transactions by type and category
- Pagination with skip and limit
- Summary for income, expense, and balance
- Request validation with Pydantic

## Tech Stack

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Pydantic
- Uvicorn

## Project Structure

```text
backend/
├── app/
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   └── routers/
│       └── transactions.py
├── requirements.txt
├── README.md
└── ROADMAP.md
## Run Tests

This project uses pytest for unit testing.

```bash
pytest
## Authentication Flow

This project uses JWT authentication.

### 1. Register

```http
POST /auth/register