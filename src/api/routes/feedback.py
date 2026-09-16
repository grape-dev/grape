from fastapi import APIRouter, Depends
from pydantic import BaseModel
from src.api.deps import get_current_user
from src.config import log
import os

router = APIRouter(prefix='/api', tags=['feedback'])

class FeedbackBody(BaseModel):
    category: str
    title: str
    description: str
    page_url: str = ''
    repo_key: str = ''
    attachment_url: str = ''

@router.post('/feedback')
def submit_feedback(body: FeedbackBody, user: dict = Depends(get_current_user)):
    '''提交问题反馈（转发到 admin 后端）。'''
    import requests
    admin_url = os.environ.get('GRAPE_ADMIN_URL', 'http://localhost:8081')
    payload = {
        'user_id': user.get('id') or 0,
        'username': user.get('username') or '',
        'category': body.category,
        'title': body.title,
        'description': body.description,
        'page_url': body.page_url or '',
        'repo_key': body.repo_key or '',
        'attachment_url': body.attachment_url or '',
    }
    try:
        resp = requests.post(f'{admin_url}/api/feedback', json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        log.warning(f'feedback submit failed: {e}')
        return {'error': '反馈提交失败，请稍后重试'}
