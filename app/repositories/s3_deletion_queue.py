from sqlalchemy.ext.asyncio import AsyncSession

from app.models import S3DeletionQueue


async def enqueue(session: AsyncSession, *, s3_key: str) -> S3DeletionQueue:
    row = S3DeletionQueue(s3_key=s3_key)
    session.add(row)
    await session.flush()
    return row
