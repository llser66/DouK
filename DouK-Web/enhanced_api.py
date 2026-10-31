"""Optional API adapter; keeps the downloaded upstream sources unchanged."""
import asyncio
import argparse
from contextvars import ContextVar
import os
from pathlib import Path
import sys

UPSTREAM = Path(__file__).resolve().parent.parent / 'TikTokDownloader'
sys.path.insert(0, str(UPSTREAM))
os.chdir(UPSTREAM)

from fastapi import Depends
from src.application import TikTokDownloader
from src.application.main_server import APIServer, token_dependency
from src.interface import Collection
from src.interface.comment import Comment as CommentInterface, Reply as ReplyInterface
from src.models.base import APIModel
from src.interface.account import Account as AccountInterface

# Local bookkeeping only; acquisition remains the upstream Account implementation.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from account_coverage import Coverage
account_coverage = ContextVar("account_coverage", default=None)
from src.models.comment import Comment as CommentModel


class UserRequest(APIModel):
    sec_user_id: str


class CommentPageRequest(CommentModel):
    comment_id: str | None = None


class CommentPage(CommentInterface):
    def check_response(self, data_dict, *args, **kwargs):
        self.page_metadata = data_dict
        return super().check_response(data_dict, *args, **kwargs)


class ReplyPage(ReplyInterface):
    def check_response(self, data_dict, *args, **kwargs):
        self.page_metadata = data_dict
        return super().check_response(data_dict, *args, **kwargs)


class CoveredAccount(AccountInterface):
    async def run_single(self, *args, **kwargs):
        self.page_seen = False
        await super().run_single(*args, **kwargs)
        if not self.page_seen:
            self.coverage.valid = False
            self.finished = True

    def check_response(self, data_dict, data_key, error_text='', cursor='cursor', has_more='has_more', *args, **kwargs):
        self.page_seen = True
        self.coverage.observe(data_dict, data_key, cursor, has_more)
        if isinstance(data_dict.get(data_key), list):
            for row in data_dict[data_key]:
                if not isinstance(row, dict) or not row.get('aweme_id') or not isinstance(row.get('create_time'), (int, float)) or row['create_time'] <= 0 or (row.get('author') or {}).get('sec_uid') != self.sec_user_id:
                    self.coverage.valid = False
        result = super().check_response(data_dict, data_key, error_text, cursor, has_more, *args, **kwargs)
        if not self.coverage.valid:
            self.finished = True
        return result

    async def early_stop(self):
        stopped = self.finished
        await super().early_stop()
        if not stopped and self.finished and self.coverage.valid:
            self.coverage.boundary = True


class WorkspaceAPI(APIServer):
    async def _get_account_data(self, cookie=None, proxy=None, sec_user_id=..., tab='post', earliest='', latest='', pages=None, *args, **kwargs):
        processor = CoveredAccount(self.parameter, cookie, proxy, sec_user_id, tab, earliest, latest, pages, **kwargs)
        processor.coverage = account_coverage.get() or Coverage()
        return await processor.run()

    async def handle_account(self, extract, tiktok=False):
        if tiktok:
            return await super().handle_account(extract, tiktok)
        coverage = Coverage()
        context = account_coverage.set(coverage)
        try:
            response = await super().handle_account(extract, tiktok)
            metadata = coverage.result()
            metadata['filtered_count'] = len(response.data) if isinstance(response.data, list) else 0
            response.params = {**(response.params or {}), 'workspace_check': metadata}
            if response.data is None and coverage.result()['complete']:
                response.data = []
                response.message = '发布列表已核查，监测期间无可见作品'
            return response
        finally:
            account_coverage.reset(context)

    def setup_routes(self):
        super().setup_routes()

        @self.server.post('/douyin/comment/page', tags=['工作台扩展'], summary='单批评论或回复，保留平台翻页游标')
        async def comment_page(extract: CommentPageRequest, token=Depends(token_dependency)):
            arguments = dict(detail_id=extract.detail_id, pages=1, cursor=extract.cursor, count=extract.count)
            if extract.comment_id:
                arguments['comment_id'] = extract.comment_id
                processor = ReplyPage(self.parameter, extract.cookie, extract.proxy, **arguments)
            else:
                processor = CommentPage(self.parameter, extract.cookie, extract.proxy, reply=False, **arguments)
            raw = await processor.run(single_page=True)
            metadata = getattr(processor, 'page_metadata', None)
            if not isinstance(metadata, dict) or not isinstance(metadata.get('comments'), list) or 'has_more' not in metadata or 'cursor' not in metadata:
                return {'data': None, 'message': '平台未返回有效评论分页，请检查账号、权限或平台限制'}
            if metadata.get('status_code', 0) != 0:
                return {'data': None, 'message': '平台拒绝了本次评论请求，请检查账号状态后重试'}
            normalized = await self.save_comment(extract.detail_id, raw) if raw else []
            return {'data': normalized, 'cursor': metadata['cursor'], 'has_more': bool(metadata['has_more']),
                    'total': metadata.get('total'), 'message': '评论分页获取成功'}

        @self.server.post('/douyin/user', tags=['工作台扩展'], summary='获取账号详细信息')
        async def user(extract: UserRequest, token=Depends(token_dependency)):
            raw = await self._get_user_data(extract.sec_user_id, extract.cookie, extract.proxy)
            data = await self._deal_user_data([raw]) if raw else None
            return self.success_response(extract, data) if data else self.failed_response(extract)

        @self.server.post('/douyin/hot', tags=['工作台扩展'], summary='获取抖音热榜')
        async def hot(extract: APIModel, token=Depends(token_dependency)):
            _, boards = await self._deal_hot_data(cookie=extract.cookie, proxy=extract.proxy)
            rows = [dict(row, board=name) for board in (boards or []) for name, entries in board.items() for row in entries]
            return self.success_response(extract, rows) if rows else self.failed_response(extract)

        @self.server.post('/douyin/collection', tags=['工作台扩展'], summary='获取当前登录账号的收藏作品')
        async def collection(extract: UserRequest, token=Depends(token_dependency)):
            info = await self.get_user_info_data(False, extract.cookie, extract.proxy, sec_user_id=extract.sec_user_id)
            if not info:
                return self.failed_response(extract, '账号信息获取失败，请填写当前登录 Cookie 对应的 sec_uid')
            raw = await Collection(self.parameter, extract.cookie, extract.proxy, extract.sec_user_id).run()
            if not raw:
                return self.failed_response(extract, '收藏为空或当前 Cookie 没有访问权限')
            data = await self._batch_process_detail(raw, True, mode='collection', user_id=extract.sec_user_id, info=info)
            return self.success_response(extract, data) if data else self.failed_response(extract)


async def main(web_start=False):
    async with TikTokDownloader() as app:
        # The web entry point runs directly; it does not record acceptance of a notice.
        if not web_start and not await app.disclaimer():
            return
        app.check_config()
        await app.check_settings(False)
        await WorkspaceAPI(app.parameter, app.database).run_server(host='127.0.0.1', port=5555)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--web-start', action='store_true')
    options = parser.parse_args()
    asyncio.run(main(options.web_start))
