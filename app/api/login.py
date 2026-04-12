import hashlib
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

VALID_USERNAME = "123123"
SALT = "kaiamu_secret_salt"
HASHED_PASSWORD = hashlib.sha256(f"kaiamu{SALT}".encode()).hexdigest()

class LoginRequest(BaseModel):
    username: str
    password: str

@router.post("/login")
async def login(request: LoginRequest):
    if request.username != VALID_USERNAME:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    hashed_input = hashlib.sha256(f"{request.password}{SALT}".encode()).hexdigest()

    if hashed_input != HASHED_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    return {"success": True, "message": "登录成功"}