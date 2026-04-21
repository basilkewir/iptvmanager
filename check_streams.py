#!/usr/bin/env python3
import asyncio
from sqlalchemy import text
from app.database import async_session
from app.models import Stream

async def check_streams():
    async with async_session() as session:
        result = await session.execute(text('SELECT id, name, source_url, rtmp_key, status FROM streams'))
        streams = result.fetchall()
        print('Current streams:')
        if not streams:
            print('  (no streams found)')
        for s in streams:
            print(f'  ID: {s.id}, Name: {s.name}, Source: {s.source_url}, RTMP: {s.rtmp_key}, Status: {s.status}')

if __name__ == "__main__":
    asyncio.run(check_streams())
