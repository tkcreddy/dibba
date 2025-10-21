from fastapi import FastAPI, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from utils.extensions.utilities_extention import UtilitiesExtension
from utils.redis.redis_interface import RedisInterface
from utils.ReadConfig import ReadConfig as rc
from logpkg.log_kcld import LogKCld
from datetime import datetime, timedelta
from utils.celery.tasks.aws_tasks import get_ec2_instances,create_worker_nodes,terminate_worker_node
import jwt
from passlib.context import CryptContext
from celery import Celery
import os
read_config=rc()
redis_config=read_config.redis_db_config
rd=RedisInterface(redis_config['redis_host'],redis_config['redis_port'],redis_config['redis_db'])
app = FastAPI()


# Secret key for JWT
key=read_config.encryption_config['key']
SECRET_KEY = key
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
ue=UtilitiesExtension(key)

# OAuth2 for authentication
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

# Password hashing
#pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Fake users database
#

# Celery Configuration
celery = Celery(
    "worker",
    broker="rediss://localhost:6379/0?ssl_cert_reqs=required&ssl_ca_certs=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fca.crt&ssl_certfile=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fclient.crt&ssl_keyfile=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fclient.key",
    backend="rediss://localhost:6379/0?ssl_cert_reqs=required&ssl_ca_certs=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fca.crt&ssl_certfile=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fclient.crt&ssl_keyfile=%2FUsers%2Fkrishnareddy%2FPycharmProjects%2Fbaks%2Fconfig%2Fssl%2Fclient.key"

)

# Authenticate User
def authenticate_user(username: str, password: str):
    if not rd.get_user_pass(username):
        return False
    if ue.encode_phrase_with_key(password) == rd.get_user_pass(username):
        return username

# Create JWT Token
def create_access_token(data: dict, expires_delta: timedelta):
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

# Token Endpoint
@app.post("/token")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(status_code=400, detail="Invalid credentials")
    access_token = create_access_token(
        data={"sub": form_data.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    return {"access_token": access_token, "token_type": "bearer"}

#Dependency to get the current user
def get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        print(username)
        if username is None or not rd.get_user_pass(username):
            raise HTTPException(status_code=401, detail="Invalid authentication")
        return username
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="Invalid token") from e

# Post a Celery Task
@app.post("/tasks/")
async def create_task(data: dict, user: str = Depends(get_current_user)):
    task = celery.send_task("app.tasks.process_task", args=[data])
    return {"task_id": task.id, "status": "Task submitted"}
