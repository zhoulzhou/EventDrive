import hashlib
from fastapi import APIRouter, HTTPException, Response, Request, Depends
from pydantic import BaseModel
from itsdangerous import URLSafeSerializer, BadData

router = APIRouter()

SECRET_KEY = "kaiamu_secure_secret_key_2024"
SESSION_COOKIE_NAME = "session_token"
serializer = URLSafeSerializer(SECRET_KEY)

VALID_USERNAME = "123123"
SALT = "kaiamu_secret_salt"
HASHED_PASSWORD = hashlib.sha256(f"kaiamu{SALT}".encode()).hexdigest()

class LoginRequest(BaseModel):
    username: str
    password: str

def create_session_token(username: str) -> str:
    return serializer.dumps({"username": username}, salt=SALT)

def verify_session_token(token: str) -> dict | None:
    try:
        return serializer.loads(token, salt=SALT)
    except BadData:
        return None

def is_logged_in(request: Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return False
    return verify_session_token(token) is not None

async def require_auth(request: Request):
    if not is_logged_in(request):
        raise HTTPException(status_code=401, detail="未登录")
    return True

@router.post("/login")
async def login(request: LoginRequest, response: Response):
    if request.username != VALID_USERNAME:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    hashed_input = hashlib.sha256(f"{request.password}{SALT}".encode()).hexdigest()

    if hashed_input != HASHED_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    session_token = create_session_token(request.username)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        httponly=True,
        max_age=86400 * 7,
        samesite="lax"
    )

    return {"success": True, "message": "登录成功"}

@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return {"success": True, "message": "登出成功"}

@router.get("/check-auth")
async def check_auth(request: Request):
    return {"authenticated": is_logged_in(request)}
