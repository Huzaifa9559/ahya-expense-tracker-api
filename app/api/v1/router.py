from fastapi import APIRouter

from app.api.v1 import auth, categories, expenses, reports

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(categories.router)
api_router.include_router(expenses.router)
api_router.include_router(reports.router)
