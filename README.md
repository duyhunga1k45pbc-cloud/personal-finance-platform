# Personal Finance API

A simple backend API for personal finance management built with FastAPI, PostgreSQL, SQLAlchemy, and Pydantic.

This project supports user authentication, protected transaction APIs, filtering, pagination, and user-based data isolation.

## Features

- Register user
- Login user with JWT
- Get current authenticated user
- Create transaction
- Get transaction list
- Get transaction detail
- Update transaction
- Delete transaction
- Filter transactions by type and category
- Pagination with skip and limit
- Summary for income, expense, and balance
- Request validation with Pydantic
- Protected APIs with JWT authentication
- User data isolation

## Tech Stack

- Python
- FastAPI
- PostgreSQL
- SQLAlchemy
- Pydantic
- JWT
- Uvicorn
- python-dotenv

## Project Structure

```text
backend/
├── app/
│   ├── main.py
│   ├── database.py
│   ├── models.py
│   ├── schemas.py
│   ├── auth.py
│   └── routers/
│       ├── auth.py
│       └── transactions.py
├── tests/
│   └── test_transactions.py
├── .env.example
├── .gitignore
├── pytest.ini
├── requirements.txt
└── README.md
```

## API Endpoints

### Auth APIs

| Method | Endpoint | Auth Required | Description |
|---|---|---|---|
| POST | `/auth/register` | No | Register a new user |
| POST | `/auth/login` | No | Login and get JWT token |
| GET | `/auth/me` | Yes | Get current authenticated user |

### Transaction APIs

| Method | Endpoint | Auth Required | Description |
|---|---|---|---|
| GET | `/transactions` | Yes | List current user's transactions |
| POST | `/transactions` | Yes | Create a transaction |
| GET | `/transactions/{transaction_id}` | Yes | Get transaction detail |
| PUT | `/transactions/{transaction_id}` | Yes | Update transaction |
| DELETE | `/transactions/{transaction_id}` | Yes | Delete transaction |
| GET | `/summary` | Yes | Get income, expense, and balance summary |

## Environment Variables

Create a `.env` file from `.env.example`:

```bash
cp .env.example .env
```

Example `.env`:

```env
DATABASE_URL=postgresql://postgres:123456@localhost/finance_db

SECRET_KEY=change-this-secret-key
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=60
```

The `.env` file is ignored by Git.

## Run Project Locally

### 1. Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create PostgreSQL database

Login to PostgreSQL:

```bash
sudo -u postgres psql
```

Create database:

```sql
CREATE DATABASE finance_db;
```

Exit PostgreSQL:

```sql
\q
```

### 4. Run server

```bash
uvicorn app.main:app --reload
```

Open Swagger docs:

```text
http://127.0.0.1:8000/docs
```

## Authentication Flow

This project uses JWT authentication.

### 1. Register

```http
POST /auth/register
```

Request body:

```json
{
  "email": "user@example.com",
  "password": "123456"
}
```

Example response:

```json
{
  "id": 1,
  "email": "user@example.com",
  "message": "User registered successfully"
}
```

### 2. Login

```http
POST /auth/login
```

Request body:

```json
{
  "email": "user@example.com",
  "password": "123456"
}
```

Example response:

```json
{
  "access_token": "jwt_token_here",
  "token_type": "bearer"
}
```

### 3. Use Token in Swagger

In Swagger UI:

1. Click **Authorize**
2. Paste the `access_token`
3. Click **Authorize**
4. Call protected APIs

Protected APIs require this header:

```http
Authorization: Bearer <access_token>
```

## Transaction Example

### Create transaction

```http
POST /transactions
```

Request body:

```json
{
  "amount": 50000,
  "description": "Dinner",
  "category": "food",
  "type": "expense"
}
```

Example response:

```json
{
  "id": 1,
  "amount": 50000,
  "description": "Dinner",
  "category": "food",
  "type": "expense",
  "user_id": 1,
  "date": "2026-07-07T10:00:00"
}
```

## Validation Rules

`type` must be one of:

```text
income
expense
```

`amount` must be greater than `0`.

Invalid request example:

```json
{
  "amount": 0,
  "description": "Invalid transaction",
  "category": "food",
  "type": "expense"
}
```

This will return:

```text
422 Unprocessable Entity
```

## Filtering and Pagination

List transactions:

```http
GET /transactions
```

Filter by type:

```http
GET /transactions?type=expense
```

Filter by category:

```http
GET /transactions?category=food
```

Filter by type and category:

```http
GET /transactions?type=expense&category=food
```

Pagination:

```http
GET /transactions?skip=0&limit=10
```

Example response:

```json
{
  "total": 2,
  "skip": 0,
  "limit": 10,
  "data": [
    {
      "id": 1,
      "amount": 50000,
      "description": "Dinner",
      "category": "food",
      "type": "expense",
      "user_id": 1,
      "date": "2026-07-07T10:00:00"
    }
  ]
}
```

## Summary

```http
GET /summary
```

Example response:

```json
{
  "total_income": 10000000,
  "total_expense": 140000,
  "balance": 9860000
}
```

## Data Isolation

Each user can only access their own transactions.

For example:

- User A can only see User A's transactions
- User B can only see User B's transactions
- Users cannot access each other's transaction data

## Run Tests

This project uses pytest for unit testing.

```bash
pytest
```

Current tested features:

- Home API
- Create transaction API
- Amount validation
- Transaction type validation

## Current Status

- Basic CRUD completed
- JWT authentication completed
- Protected routes completed
- User data isolation completed
- Filter completed
- Pagination completed
- Summary completed
- Environment configuration completed
- Basic unit tests completed

## Run with Docker

Create `.env` file:

```bash
cp .env.example .env
```

For Docker, update `DATABASE_URL`:

```env
DATABASE_URL=postgresql://postgres:123456@db:5432/finance_db
```

Run project:

```bash
docker compose up --build -d
```

Open Swagger:

```text
http://127.0.0.1:8000/docs
```

Stop project:

```bash
docker compose down
```
## Database Migration with Alembic

This project uses Alembic to manage database schema changes.

Create or update database tables:

```bash
alembic upgrade head
```

Create a new migration after changing SQLAlchemy models:

```bash
alembic revision --autogenerate -m "migration message"
```

Check current migration version:

```bash
alembic current
```

Rollback one migration:

```bash
alembic downgrade -1
```