# Baks



## **Project Analysis**
### **Overview**
This project is a **FastAPI application** designed to:
- Manage AWS EC2 instances through an asynchronous workflow using **Celery** and **Redis**.
- Secure the API using **OAuth2 with JWT-based authentication**.
- Provide endpoint-based APIs to:
    - **Create EC2 instances** and store their metadata in Redis for tracking.
    - **Terminate EC2 instances by namespace**.
    - **Monitor and query Celery task statuses**.

### **Components of the Project**
The following components come together to build the system:

| **Component** | **Description** |
| --- | --- |
| **Authentication** | Uses OAuth2 with JWT for securing private routes, implemented using FastAPI's inbuilt `Depends`. |
| **Celery Task Management** | Handles long-running tasks (e.g., EC2 instance creation/termination) asynchronously. |
| **Redis Integration** | Manages caching, task metadata, and application data like user passwords via Redis. |
| **AWS EC2 Operations** | Abstracted in Celery tasks to create or terminate EC2 instances using AWS SDK (`boto3`). |
| **Logging** | Custom logging implemented using `logpkg.log_kcld` for both file logging and console tracking. |
### **Project Structure**
``` 
project/
├── main_api.py                   # Main FastAPI application with routing and core logic
├── requirements.txt              # Python dependencies
├── utils/                        # Utility folder for Celery, Configs, and Redis Integrations
│   ├── ReadConfig.py             # Reads app configuration (AWS, Redis, etc.)
│   ├── celery/
│   │   ├── celery_config.py      # Celery app configuration (broker, backend settings, etc.)
│   │   ├── tasks/
│   │   │   ├── aws_tasks.py      # AWS EC2-related Celery tasks for creating/terminating instances
│   └── redis/
│       ├── redis_interface.py    # Redis Interface for saving/retrieving keys/nodes.
├── logpkg/                       # Logging utilities
│   ├── log_kcld.py               # Custom logging implementation
├── docs/                         # Documentation directory
│   ├── README.md                 # Overview of the application
│   ├── authentication.md         # Details about authentication (OAuth2 + JWT)
│   ├── api_endpoints.md          # Detailed API routes and usage
│   ├── architecture.md           # Technical architecture and workflows
├── tests/                        # Unit tests for APIs, Redis, and Celery
```
### **Technology Stack**
- **Framework**: FastAPI (Python 3)
- **Task Queue**: Celery
- **Broker**: Redis
- **Database**: Redis (used for caching and storing instance/user data)
- **AWS SDK**: `boto3` for EC2 management
- **JWT**: For authentication
- **Logging**: Custom logging built with `logpkg`

### **Core Features**
1. **Authentication**:
    - Uses OAuth2 and JWT for securing the APIs.
    - Token-based implementation with `/token` endpoint for generating access tokens.
    - Token expiration configured for 30 minutes.

2. **AWS EC2 Management**:
    - Create EC2 instances using `/create-instances/` API.
    - Terminate EC2 instances by namespace via `/terminate-namespace/`.

3. **Task Monitoring**:
    - Monitor any Celery task's status by querying `/task/{task_id}`.
    - Tasks can result in `PENDING`, `SUCCESS`, or `FAILURE` states.

4. **Redis Integration**:
    - Stores instance metadata (e.g., IPs, IDs) and user hashed passwords.

5. **Custom Logging**:
    - Logs critical API events, errors, and task statuses using `log_kcld`.

## **Documentation for the `docs/` Directory**
These files will contain detailed documentation for developers working on or using the project:
### 1. `README.md`
``` markdown
# FastAPI AWS EC2 Manager

This project is a FastAPI-based web application designed to manage **AWS EC2 instances** asynchronously with **Celery** and **Redis**. It provides APIs for EC2 instance creation, termination, and task monitoring, ensuring secure access through OAuth2-based authentication.

---

## **Features**
1. **OAuth2 Authentication**:
    - JWT-based token system to protect APIs.
    - `/token` endpoint for generating access tokens.
2. **Task Management**:
    - Uses Celery for creating and terminating EC2 instances asynchronously.
3. **Redis Integration**:
    - Centralized storage for:
      - Celery task results and progress tracking.
      - Instance metadata (e.g., IPs, IDs, namespaces).
4. **Logging**:
    - In-depth logging of task operations and errors.

---

## **Quickstart**

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Start Redis:
   ```bash
   redis-server
   ```

3. Start Celery workers:
   ```bash
   celery -A utils.celery.celery_config.celery_app worker --loglevel=info
   ```

4. Start FastAPI server:
   ```bash
   uvicorn main_api:app --host 0.0.0.0 --port 8000
   ```

For detailed API usage, see [api_endpoints.md](./api_endpoints.md)!

---
```
### 2. `authentication.md`
``` markdown
# Authentication

This application uses OAuth2 with JWT for authenticating users.

## **Endpoints**
1. **`/token` (POST)**:
   - URL: `/token`
   - Description: Accepts username and password, then generates a Bearer JWT token.
   - **Request Body**:
     ```json
     {
       "username": "string",
       "password": "string"
     }
     ```
   - **Response**:
     ```json
     {
       "access_token": "string",
       "token_type": "bearer"
     }
     ```

---

### **Token Expiry**
- Tokens are valid for **30 minutes** from the time of creation.
- If expired, you need to reauthenticate via `/token`.

---

### **Securing API Calls**
Secured endpoints require an `Authorization` header:
```
