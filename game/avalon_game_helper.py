"""
游戏辅助模块 - 提供辅助功能供玩家代码使用
"""

import os
import json
import time
import logging
import threading
from typing import Dict, Any, List, Tuple, Optional
from dotenv import load_dotenv
from .client_manager import ClientManager, get_client_manager

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("GameHelper")


# LLM相关配置
_USE_STREAM = False  # 使用流式
_INIT_SYSTRM_PROMPT = """
你是一个专业助理。
"""  # 后期可修改
_TEMPERATURE = 1  # 创造性 (0-2, 默认1)
_MAX_INPUT_TOKENS = 500  # 最大 prompt 长度
_MAX_OUTPUT_TOKENS = 500  # 最大生成长度
_MAX_CALL_COUNT_PER_ROUND = 888  # 一轮最多调用 LLM 次数
_TOP_P = 0.9  # 输出多样性控制
_PRESENCE_PENALTY = 0.5  # 避免重复话题 (-2~2)
_FREQUENCY_PENALTY = 0.5  # 避免重复用词 (-2~2)


# 初始用户库JSON
INIT_PRIVA_LOG_DICT = {
    "logs": [],
    "llm_history": [{"role": "system", "content": _INIT_SYSTRM_PROMPT}],
    "llm_call_counts": [0 for _ in range(6)],  # 第 1~5 轮分别用了几次
}


class GameHelper:
    """游戏辅助类，管理LLM调用和日志功能"""

    def __init__(self, data_dir=None):
        self.current_player_id = None
        self.game_session_id = None
        self.data_dir = data_dir or os.environ.get("AVALON_DATA_DIR", "./data")
        self.current_round = None
        self.call_count_added = 0
        self.tokens = [{"input": 0, "output": 0} for i in range(7)]
        self.client_manager = get_client_manager()
        self.observer = None

    def set_current_context(self, player_id: int, game_id: str) -> None:
        """
        设置当前上下文 - 这个函数由 referee 在调用玩家代码前设置

        参数:
            player_id: 当前玩家 ID
            game_id: 当前游戏会话 ID
        """
        self.current_player_id = player_id
        self.game_session_id = game_id

    def reset_llm_limit(self, round_: int) -> None:
        """
        公投未通过，重置本轮llm调用限制
        """
        # 获取日志
        existing_data = self._get_private_lib_content()
        # 获取LLM调用次数记录 - 使用传入的round_参数
        player_call_counts = existing_data["llm_call_counts"][round_]
        # 同时更新当前轮次，确保一致性
        self.current_round = round_
        self.call_count_added += player_call_counts

    def set_current_round(self, round_: int) -> None:
        """
        设置当前 ROUND 上下文 - 这个函数由 referee 在更改 ROUND 时设置

        参数:
            round_: 当前游戏运行到第几轮
        """
        self.current_round = round_
        # 重置本轮追加llm调用次数
        self.call_count_added = 0

    def askLLM(self, prompt: str) -> str:
        """
        向大语言模型发送提示并获取回答

        参数:
            prompt: 发送给LLM的提示文本

        返回:
            LLM的回答文本, 或在错误时返回描述性错误信息
        """
        if not self.current_player_id or not self.game_session_id:
            logger.error("LLM调用缺少上下文（玩家ID或游戏ID缺失）")
            return "LLM调用错误：未设置玩家或游戏上下文"

        # 获取日志
        existing_data = self._get_private_lib_content()
        # 获取LLM聊天记录
        player_chat_history = existing_data["llm_history"]
        # 获取LLM调用次数记录
        player_call_counts = existing_data["llm_call_counts"]

        # 判断用户在这一轮已经调用几次 LLM，执行相应操作
        if (
            player_call_counts[self.current_round]
            > _MAX_CALL_COUNT_PER_ROUND + self.call_count_added
        ):
            raise RuntimeError(
                f"Maximum call count per round of player {self.current_player_id} exceeded"
            )
        else:
            existing_data["llm_call_counts"][self.current_round] += 1

        # if len(prompt) > _MAX_INPUT_TOKENS:
        #     prompt = prompt[:_MAX_INPUT_TOKENS]  # 切断 prompt 至限制长度以内
        #     logger.warning(
        #         f"{self.current_player_id}号玩家输入 prompt 过长。"
        #         + f"只取前 {_MAX_INPUT_TOKENS} 个 token 询问 LLM。"
        #     )

        # 调LLM
        try:
            reply = self._fetch_LLM_reply(player_chat_history, prompt)
        except Exception as e:
            return f"LLM调用错误: {str(e)}"

        # 追加新日志
        try:
            existing_data["llm_history"].append({"role": "user", "content": prompt})
            existing_data["llm_history"].append({"role": "assistant", "content": reply})
        except Exception as e:
            return f"LLM聊天记录保存错误: {str(e)}"

        # 写回私有库文件
        self._write_back_private(data=existing_data)

        # 更新token统计
        token = len(prompt)
        self.tokens[self.current_player_id - 1]["input"] += token

        return reply

    def _fetch_LLM_reply(self, history, cur_prompt) -> str:
        """
        从历史记录和当前提示中获取LLM回复。
        """
        logger.info(
            f"Player {self.current_player_id} requesting LLM with prompt length {len(cur_prompt)}"
        )

        client_instance, client_id, client_model_name = None, None, None

        try:
            # 获取客户端，不设置超时
            client_instance, client_id, client_model_name = (
                self.client_manager.get_client()
            )

            if client_instance is None:
                logger.error(
                    f"Player {self.current_player_id} failed to get an OpenAI client"
                )
                return "LLM调用错误：没有可用的OpenAI客户端"

            logger.info(f"Player {self.current_player_id} using client {client_id}")

            import time

            start_time = time.time()

            # 直接调用API，不使用ThreadPoolExecutor和超时参数
            completion = client_instance.chat.completions.create(
                model=client_model_name,
                messages=history + [{"role": "user", "content": cur_prompt}],
                stream=False,
                temperature=_TEMPERATURE,
                max_tokens=_MAX_OUTPUT_TOKENS,
                top_p=_TOP_P,
                presence_penalty=_PRESENCE_PENALTY,
                frequency_penalty=_FREQUENCY_PENALTY,
                # 移除超时参数
            )

            response_content = completion.choices[0].message.content

            elapsed = time.time() - start_time
            token = len(response_content)
            self.tokens[self.current_player_id - 1]["output"] += token

            logger.info(
                f"Player {self.current_player_id} received response in {elapsed:.2f}s"
            )

            return response_content or "LLM调用未返回有效结果"

        except Exception as e:
            logger.error(
                f"Player {self.current_player_id} error: {str(e)}", exc_info=True
            )
            return f"LLM调用错误: {str(e)[:100]}..."
        finally:
            # 释放客户端
            if client_id is not None:
                try:
                    self.client_manager.release_client(client_id)
                    logger.info(f"Released client {client_id}")
                except Exception as e:
                    logger.error(f"Error releasing client {client_id}: {str(e)}")

    def _get_private_lib_content(self) -> dict:
        """
        获取私有库内容

        该函数用于构建私有数据文件路径，并读取现有数据。
        如果文件不存在或无法解析，将返回默认数据结构。

        返回:
            dict: 包含现有数据或默认数据结构的字典。
        """
        # 构建私有数据文件路径
        private_file = os.path.join(
            self.data_dir,
            f"game_{self.game_session_id}_player_{self.current_player_id}_private.json",
        )

        # 确保目录存在
        os.makedirs(os.path.dirname(private_file), exist_ok=True)

        # 读取现有数据
        existing_data = {}
        if os.path.exists(private_file):
            try:
                with open(private_file, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
            except json.JSONDecodeError:
                existing_data = INIT_PRIVA_LOG_DICT
        else:
            existing_data = INIT_PRIVA_LOG_DICT

        return existing_data

    def _write_back_private(self, data: dict) -> None:
        """统一处理：写回私有库 JSON 文件"""
        # 构建私有数据文件路径
        private_file = os.path.join(
            self.data_dir,
            f"game_{self.game_session_id}_player_{self.current_player_id}_private.json",
        )

        # 确保目录存在
        os.makedirs(os.path.dirname(private_file), exist_ok=True)

        # 打开文件，写回
        with open(private_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def read_private_lib(self) -> List[str]:
        """从私有库中读取内容"""
        if self.current_player_id is None or not self.game_session_id:
            logger.error("尝试在无玩家ID或游戏ID上下文的情况下读取私有日志")
            return []

        try:
            existing_data = self._get_private_lib_content()  # 获取日志
            return existing_data["logs"]
        except Exception as e:
            logger.error(f"读取私有日志时出错: {str(e)}")
            return []

    def write_into_private(self, content: str) -> None:
        """
        向当前玩家的私有存储中追加写入内容

        参数:
            content: 需要保存的文本内容
        """
        if self.current_player_id is None or not self.game_session_id:
            logger.error("尝试在无玩家ID或游戏ID上下文的情况下写入私有日志")
            return

        try:
            # 获取日志
            existing_data = self._get_private_lib_content()

            # 追加新日志
            existing_data["logs"].append({"timestamp": time.time(), "content": content})

            # 写回文件
            self._write_back_private(data=existing_data)

        except Exception as e:
            logger.error(f"写入私有日志时出错: {str(e)}")

    def read_public_lib(self) -> Dict[str, Any]:
        """
        读取当前游戏的公共历史记录

        返回:
            游戏历史记录字典
        """
        if not self.game_session_id:
            logger.error("尝试在无游戏ID上下文的情况下读取游戏历史")
            return {"error": "未设置游戏上下文", "events": []}

        try:
            public_file = os.path.join(
                self.data_dir, f"game_{self.game_session_id}_public.json"
            )

            if os.path.exists(public_file):
                with open(public_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            else:
                return {"error": "找不到游戏历史文件", "events": []}

        except Exception as e:
            logger.error(f"读取游戏历史时出错: {str(e)}")
            return {"error": str(e), "events": []}

    def get_tokens(self) -> List[Dict[str, int]]:
        return self.tokens


_thread_local = threading.local()


def get_current_helper():
    """获取当前线程的 GameHelper 实例"""
    if not hasattr(_thread_local, "helper"):
        _thread_local.helper = GameHelper()
    return _thread_local.helper


def set_thread_helper(helper):
    """设置当前线程的 GameHelper 实例"""
    _thread_local.helper = helper


# 修改为使用线程本地存储的 helper 实例
def set_current_context(player_id: int, game_id: str) -> None:
    get_current_helper().set_current_context(player_id, game_id)


def reset_llm_limit(round_: int) -> None:
    get_current_helper().reset_llm_limit(round_)


def set_current_round(round_: int) -> None:
    get_current_helper().set_current_round(round_)


def askLLM(prompt: str) -> str:
    return get_current_helper().askLLM(prompt)


def read_private_lib() -> List[str]:
    return get_current_helper().read_private_lib()


def write_into_private(content: str) -> None:
    get_current_helper().write_into_private(content)


def read_public_lib() -> Dict[str, Any]:
    return get_current_helper().read_public_lib()


if __name__ == "__main__":
    helper = GameHelper()
    print(
        helper._fetch_LLM_reply(  # 测试LLM
            history=[{"role": "system", "content": "你是一个专业助理"}],
            cur_prompt="L3自动驾驶自行车",
        )
    )
