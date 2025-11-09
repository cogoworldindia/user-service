from fastapi import APIRouter
# Create a master router that includes all route modules
# from app.controllers import email_auth_controller
from app.controllers.user_controller import router as user_router

router = APIRouter()
router.include_router(user_router, prefix="/user", tags=["Users"])
