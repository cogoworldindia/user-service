# 🧑‍💼 User Service

The **User Service** manages core user data such as user accounts, profiles, and personal information.  
It is designed as a microservice using **FastAPI**, **SQLAlchemy (Async)**, **Alembic**, and **Redis**.

---

## 🚀 Features

- Create, update, and fetch user records  
- Manage user profiles (name, DOB, gender, photo)  
- Database versioning and migration via Alembic  
- Standardized API response format for all endpoints  
- Redis integration for caching (optional use in later phases)  
- Environment-driven configuration and port management  
- Async database operations using SQLAlchemy 2.0 style

---

## 🏗️ Tech Stack

| Component | Technology |
|------------|-------------|
| Framework | FastAPI |
| ORM | SQLAlchemy (Async) |
| Migrations | Alembic |
| Cache | Redis |
| Database | PostgreSQL |
| Config | Pydantic Settings |
| Runtime | Uvicorn |

---

## 📂 Project Structure

app/
├── main.py                    
├── core/
│   ├── config.py               
├── db/
│   ├── session.py              
│   ├── init_db.py              
│   ├── migrations/             
├── models/
│   ├── user_model.py           
│   ├── user_profile_model.py   
├── schemas/
│   ├── user_schema.py          
│   ├── user_profile_schema.py  
├── services/
│   ├── user_service.py         
├── controllers/
│   ├── user_controller.py      
├── middleware/
│   ├── response_middleware.py  
├── utils/
│   ├── response_helper.py      
.env                            
alembic.ini                     

---

## ⚙️ Environment Variables

| Variable | Description | Example |
|-----------|--------------|----------|
| DATABASE_URL | PostgreSQL connection URL | postgresql+asyncpg://user:pass@localhost:5432/user_db |
| REDIS_URL | Redis connection string | redis://localhost:6379/0 |
| APP_PORT | Port to run the FastAPI service | 8081 |
| ENVIRONMENT | Environment name | dev or prod |

---

## 🧱 Database Schema

### users table
| Column | Type | Description |
|---------|------|-------------|
| id | UUID (PK) | Primary key |
| status | ENUM('active', 'inactive', 'deleted') | Account state |
| created_at | TIMESTAMP | Record creation time |
| updated_at | TIMESTAMP | Record update time |

### user_profiles table
| Column | Type | Description |
|---------|------|-------------|
| id | UUID (PK) | Primary key |
| user_id | FK → users.id | Linked user |
| full_name | VARCHAR(255) | User name |
| date_of_birth | DATE | DOB |
| gender | ENUM('male', 'female', 'other') | Gender |
| profile_photo | TEXT | Optional |
| created_at | TIMESTAMP | Created time |
| updated_at | TIMESTAMP | Updated time |

---

## 🧩 API Endpoints

| Method | Endpoint | Description |
|---------|-----------|-------------|
| POST | /v1/users | Create a new user |
| GET | /v1/users/{user_id} | Fetch full user (with profile) |
| PUT | /v1/users/{user_id} | Update user |
| POST | /v1/users/{user_id}/profile | Create profile |
| PUT | /v1/users/{user_id}/profile | Update profile |
| GET | /health | Health check |

---

## 🔄 API Response Format (Standardized)

All responses follow the same JSON structure:

### ✅ Success
{
  "status": "success",
  "code": 200,
  "message": "Request successful",
  "data": {...},
  "error": null
}

### ❌ Error
{
  "status": "error",
  "code": 404,
  "message": "User not found",
  "data": null,
  "error": "NotFoundError"
}

---

## 🧰 Running the Service

### 1️⃣ Setup environment
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/user_service
REDIS_URL=redis://localhost:6379/0
APP_PORT=8081

### 2️⃣ Install dependencies
pip install -r requirements.txt

### 3️⃣ Initialize database
alembic upgrade head

### 4️⃣ Run the app
python app/main.py

or directly via uvicorn:
uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload

---

## 🧪 Testing
curl -X GET http://localhost:8081/v1/users/<user_id>

---

## 🩺 Health Check
GET /health
Response:
{
  "status": "success",
  "code": 200,
  "message": "Request successful",
  "data": { "service": "user_service", "status": "ok" },
  "error": null
}

---

## 🧾 License
MIT License

---

## 👨‍💻 Author
**Arjeet Tekam**  
Cloud-native backend & microservices engineer 🚀