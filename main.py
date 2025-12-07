import os
import json
import re
import random
import asyncio
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Dict, List, Optional, Union, Any
import httpx
import aiofiles

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.message_components import *
from astrbot.api import AstrBotConfig

# ============================= 核心词库类 =============================
class KeywordManager:
    def __init__(self, config: Dict, data_dir: str):
        self.config = config
        self.data_dir = data_dir
        self.lexicons: Dict[str, Dict] = {}
        self.cooling_data: Dict[str, List] = {}
        self.coins_data: Dict[str, List] = {}
        self.switch_config: Dict[str, str] = {}
        self.select_config: Dict[str, str] = {}
        
        # 确保目录存在
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "lexicon"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "cooling"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "config"), exist_ok=True)
        os.makedirs(os.path.join(data_dir, "filecache"), exist_ok=True)
        
        # 加载初始配置
        self.load_configs()
    
    def load_configs(self):
        """加载配置文件"""
        # 加载开关配置
        switch_path = os.path.join(self.data_dir, "switch.txt")
        if os.path.exists(switch_path):
            with open(switch_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        self.switch_config[k.strip()] = v.strip()
        
        # 加载选择配置
        select_path = os.path.join(self.data_dir, "select.txt")
        if os.path.exists(select_path):
            with open(select_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if '=' in line:
                        k, v = line.split('=', 1)
                        self.select_config[k.strip()] = v.strip()
    
    async def get_lexicon(self, group_id: str, user_id: str = "") -> Dict:
        """获取词库数据"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon_path = os.path.join(self.data_dir, "lexicon", f"{lexicon_id}.json")
        
        if lexicon_id in self.lexicons:
            return self.lexicons[lexicon_id]
        
        try:
            if os.path.exists(lexicon_path):
                async with aiofiles.open(lexicon_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    data = json.loads(content)
                    self.lexicons[lexicon_id] = data
                    return data
        except Exception as e:
            logger.error(f"加载词库失败 {lexicon_id}: {e}")
        
        # 创建空词库
        empty_data = {"work": []}
        self.lexicons[lexicon_id] = empty_data
        return empty_data
    
    def get_lexicon_id(self, group_id: str, user_id: str = "") -> str:
        """获取词库ID"""
        if user_id and user_id in self.select_config:
            return self.select_config[user_id]
        
        # 检查群开关
        if group_id in self.switch_config and self.switch_config[group_id]:
            return self.switch_config[group_id]
        
        return group_id
    
    async def save_lexicon(self, lexicon_id: str, data: Dict):
        """保存词库"""
        lexicon_path = os.path.join(self.data_dir, "lexicon", f"{lexicon_id}.json")
        self.lexicons[lexicon_id] = data
        
        async with aiofiles.open(lexicon_path, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(data, indent=4, ensure_ascii=False))
    
    async def search_keyword(self, text: str, group_id: str, user_id: str, is_admin: bool = False) -> Optional[Union[str, List]]:
        """搜索关键词"""
        lexicon = await self.get_lexicon(group_id, user_id)
        current_lexicon_id = self.get_lexicon_id(group_id, user_id)
        
        # 需要检查两个词库：当前群词库和全局词库
        lexicon_ids = [current_lexicon_id]
        if current_lexicon_id != group_id:
            lexicon_ids.append(group_id)
        
        for lid in lexicon_ids:
            lex_data = await self.get_lexicon(lid, "")
            for idx, item in enumerate(lex_data.get("work", [])):
                for key, value in item.items():
                    # 检查管理员模式
                    if value.get("s") == 10 and not is_admin:
                        continue
                    
                    # 检查通配符匹配
                    if "[n." in key:
                        match_result = self.match_wildcard(key, text)
                        if match_result:
                            return {
                                "type": "wildcard",
                                "response": random.choice(value["r"]),
                                "matches": match_result,
                                "lexicon_id": lid,
                                "item_index": idx
                            }
                    
                    # 精确匹配
                    if value.get("s") == 1 and key == text:
                        return {
                            "type": "exact",
                            "response": random.choice(value["r"]),
                            "lexicon_id": lid,
                            "item_index": idx
                        }
                    
                    # 模糊匹配
                    if value.get("s") == 0 and key in text:
                        return {
                            "type": "fuzzy",
                            "response": random.choice(value["r"]),
                            "lexicon_id": lid,
                            "item_index": idx
                        }
        
        return None
    
    def match_wildcard(self, pattern: str, text: str) -> Optional[List[str]]:
        """通配符匹配 [n.?]"""
        # 将[n.1]等转换为正则表达式
        safe_pattern = re.escape(pattern)
        safe_pattern = re.sub(r'\\\[n\\.(\d+)\\\]', r'(.+?)', safe_pattern)
        
        try:
            match = re.match(f"^{safe_pattern}$", text)
            if match:
                # 提取所有匹配组
                groups = match.groups()
                result = ["", "", "", "", "", ""]
                # 提取通配符编号
                placeholders = re.findall(r'\[n\.(\d+)\]', pattern)
                for idx, ph in enumerate(placeholders):
                    ph_idx = int(ph)
                    if ph_idx < len(result) and idx < len(groups):
                        result[ph_idx] = groups[idx]
                return result
        except re.error as e:
            logger.error(f"正则匹配错误: {e}")
        
        return None
    
    async def check_cooling(self, user_id: str, group_id: str, lexicon_id: str, item_index: int) -> Union[bool, int]:
        """检查冷却时间"""
        cooling_path = os.path.join(self.data_dir, "cooling", f"{group_id}.txt")
        
        if not os.path.exists(cooling_path):
            return False
        
        current_time = datetime.now().timestamp()
        try:
            async with aiofiles.open(cooling_path, 'r', encoding='utf-8') as f:
                lines = await f.readlines()
                for line in lines:
                    parts = line.strip().split('=')
                    if len(parts) == 3:
                        uid, idx_str, expire_str = parts
                        if uid == user_id and int(idx_str) == item_index:
                            expire_time = float(expire_str)
                            if current_time >= expire_time:
                                # 冷却已结束
                                return False
                            else:
                                # 返回剩余秒数
                                return int(expire_time - current_time)
        except Exception as e:
            logger.error(f"检查冷却失败: {e}")
        
        return False
    
    async def set_cooling(self, user_id: str, group_id: str, lexicon_id: str, item_index: int, seconds: int):
        """设置冷却时间"""
        cooling_path = os.path.join(self.data_dir, "cooling", f"{group_id}.txt")
        
        current_time = datetime.now().timestamp()
        expire_time = current_time + seconds
        
        lines = []
        updated = False
        
        if os.path.exists(cooling_path):
            async with aiofiles.open(cooling_path, 'r', encoding='utf-8') as f:
                lines = await f.readlines()
        
        new_lines = []
        for line in lines:
            parts = line.strip().split('=')
            if len(parts) == 3:
                uid, idx_str, expire_str = parts
                if uid == user_id and int(idx_str) == item_index:
                    # 更新现有记录
                    new_lines.append(f"{user_id}={item_index}={expire_time}\n")
                    updated = True
                else:
                    # 保留其他记录（检查是否过期）
                    if float(expire_str) > current_time:
                        new_lines.append(line)
        
        if not updated:
            new_lines.append(f"{user_id}={item_index}={expire_time}\n")
        
        async with aiofiles.open(cooling_path, 'w', encoding='utf-8') as f:
            await f.write(''.join(new_lines))
    
    async def process_response(self, response: str, matches: Optional[List[str]], event: AstrMessageEvent) -> MessageChain:
        """处理响应文本，替换变量和特殊语法"""
        if isinstance(response, dict):
            # 通配符匹配结果
            base_response = response["response"]
            matches = response.get("matches", [])
        else:
            base_response = response
            matches = matches or []
        
        text = base_response
        
        # 替换通配符 [n.?]
        if matches:
            for i in range(1, 6):
                if i < len(matches) and matches[i]:
                    text = text.replace(f"[n.{i}]", matches[i])
                    # 提取纯文本版本（去掉特殊字符）
                    clean_match = re.search(r'[\d\w/.:?=&-]+', matches[i])
                    if clean_match:
                        text = text.replace(f"[n.{i}.t]", clean_match.group())
        
        # 替换用户变量
        text = text.replace("[qq]", str(event.get_sender_id()))
        text = text.replace("[group]", str(event.get_group_id() or ""))
        text = text.replace("[ai]", str(event.get_bot_id()))
        text = text.replace("[name]", event.get_sender_name())
        text = text.replace("[card]", event.get_sender_name())  # AstrBot中暂不支持获取群名片
        
        # 替换消息ID
        text = text.replace("[id]", str(event.message_obj.message_id))
        text = text.replace("[消息id]", str(event.message_obj.message_id))
        
        # 处理随机数 (1-100)
        while True:
            match = re.search(r'\((\d+)-(\d+)\)', text)
            if not match:
                break
            min_val = int(match.group(1))
            max_val = int(match.group(2))
            rand_num = random.randint(min_val, max_val)
            text = text.replace(match.group(0), str(rand_num), 1)
        
        # 处理时间变量
        now = datetime.now()
        time_replacements = {
            r'\(Y\)': str(now.year),
            r'\(M\)': str(now.month),
            r'\(D\)': str(now.day),
            r'\(h\)': str(now.hour),
            r'\(m\)': str(now.minute),
            r'\(s\)': str(now.second)
        }
        
        for pattern, replacement in time_replacements.items():
            text = re.sub(pattern, replacement, text)
        
        # 处理计算表达式 (+1+2*3)
        while True:
            match = re.search(r'\(\+([^)]+)\)', text)
            if not match:
                break
            expr = match.group(1)
            try:
                # 替换中文运算符
                expr = expr.replace('×', '*').replace('÷', '/')
                result = eval(expr)
                if isinstance(result, float) and result.is_integer():
                    result = int(result)
                text = text.replace(match.group(0), str(result), 1)
            except:
                # 计算失败，保留原表达式
                break
        
        # 处理冷却时间设置 (3600~)
        cooling_match = re.search(r'\((\d+)~\)', text)
        if cooling_match:
            seconds = int(cooling_match.group(1))
            if seconds == 0:
                # 次日0点
                tomorrow = datetime.now() + timedelta(days=1)
                tomorrow_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
                seconds = int(tomorrow_midnight.timestamp() - datetime.now().timestamp())
            text = re.sub(r'\(\d+~\)', '', text)
            # 这里需要上下文来设置冷却，将在主流程中处理
        
        # 处理条件判断 {a>b}
        match_compare = re.search(r'\{(.*?)([><=])(.*?)\}', text)
        if match_compare:
            a = match_compare.group(1)
            op = match_compare.group(2)
            b = match_compare.group(3)
            result = False
            
            try:
                a_val = int(a) if a.isdigit() else a
                b_val = int(b) if b.isdigit() else b
                
                if op == '>':
                    result = a_val > b_val
                elif op == '<':
                    result = a_val < b_val
                elif op == '=':
                    result = str(a_val) == str(b_val)
            except:
                result = False
            
            if result:
                text = re.sub(r'\{.*?[><=].*?\}', '', text)
            else:
                return None  # 条件不满足，不发送消息
        
        # 处理特殊指令
        return await self.parse_special_commands(text, event)
    
    async def parse_special_commands(self, text: str, event: AstrMessageEvent) -> MessageChain:
        """解析特殊指令，如图片、艾特等"""
        chain = MessageChain()
        
        # 分割消息段
        parts = re.split(r'(\[.*?\])', text)
        
        for part in parts:
            if not part.strip():
                continue
            
            if part.startswith('[') and part.endswith(']'):
                # 处理特殊指令
                cmd = part[1:-1]
                cmd_parts = cmd.split('.')
                
                if len(cmd_parts) >= 2:
                    cmd_type = cmd_parts[0].lower()
                    
                    if cmd_type in ["image", "图片"]:
                        # [图片.url]
                        url = '.'.join(cmd_parts[1:])
                        if url.startswith(('http://', 'https://')):
                            chain.append(Image.fromURL(url))
                        else:
                            chain.append(Image.fromFileSystem(url))
                    
                    elif cmd_type in ["at", "艾特"]:
                        # [艾特.123456]
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            qq = cmd_parts[1]
                            chain.append(At(qq=qq))
                        else:
                            chain.append(At(qq=str(event.get_sender_id())))
                    
                    elif cmd_type in ["face", "表情"]:
                        # [表情.123]
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            face_id = cmd_parts[1]
                            chain.append(Face(id=face_id))
                    
                    elif cmd_type in ["reply", "回复"]:
                        # [回复] 或 [回复.123]
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            msg_id = cmd_parts[1]
                            chain.append(Reply(message_id=msg_id))
                        else:
                            chain.append(Reply(message_id=event.message_obj.message_id))
                    
                    elif cmd_type in ["record", "语音"]:
                        # [语音.url]
                        url = '.'.join(cmd_parts[1:])
                        chain.append(Record(file=url))
                    
                    elif cmd_type == "poke":
                        # [poke.123456.123456]
                        if len(cmd_parts) >= 3:
                            target_id = cmd_parts[1]
                            group_id = cmd_parts[2]
                            chain.append(Poke(qq=target_id))
                    
                    else:
                        # 未知指令，作为普通文本
                        chain.append(Plain(part))
            else:
                # 普通文本
                chain.append(Plain(part))
        
        return chain
    
    # 管理功能
    async def add_keyword(self, group_id: str, user_id: str, keyword: str, response: str, mode: int = 0):
        """添加关键词"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        # 检查是否已存在
        for item in lexicon["work"]:
            if keyword in item:
                return False, "词条已存在"
        
        # 清理特殊字符（如果开启容错）
        if self.config.get("mistake_turn_type", False):
            keyword = (keyword.replace('【', '[').replace('】', ']')
                      .replace('（', '(').replace('）', ')')
                      .replace('｛', '{').replace('｝', '}').replace('：', ':'))
        
        # 添加新词条
        new_item = {keyword: {"r": [response], "s": mode}}
        lexicon["work"].append(new_item)
        
        await self.save_lexicon(lexicon_id, lexicon)
        return True, "添加成功"
    
    async def remove_keyword(self, group_id: str, user_id: str, keyword: str):
        """删除关键词"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        new_work = [item for item in lexicon["work"] if keyword not in item]
        
        if len(new_work) == len(lexicon["work"]):
            return False, "词条不存在"
        
        lexicon["work"] = new_work
        await self.save_lexicon(lexicon_id, lexicon)
        return True, "删除成功"
    
    async def add_response(self, group_id: str, user_id: str, keyword: str, response: str):
        """为关键词添加新回复"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        for item in lexicon["work"]:
            if keyword in item:
                item[keyword]["r"].append(response)
                await self.save_lexicon(lexicon_id, lexicon)
                return True, "添加成功"
        
        return False, "词条不存在"
    
    async def remove_response(self, group_id: str, user_id: str, keyword: str, response: str):
        """删除关键词的某个回复"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        for item in lexicon["work"]:
            if keyword in item and response in item[keyword]["r"]:
                item[keyword]["r"].remove(response)
                # 如果没有回复了，删除整个词条
                if not item[keyword]["r"]:
                    lexicon["work"].remove(item)
                await self.save_lexicon(lexicon_id, lexicon)
                return True, "删除成功"
        
        return False, "词条或回复不存在"
    
    async def list_keywords(self, group_id: str, user_id: str, keyword_filter: str = "") -> List[str]:
        """列出关键词"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        results = []
        for idx, item in enumerate(lexicon["work"]):
            for key, value in item.items():
                if not keyword_filter or keyword_filter in key:
                    mode_str = {
                        0: "模糊",
                        1: "精准",
                        10: "管理"
                    }.get(value["s"], "未知")
                    results.append(f"{idx+1}. {key} ({mode_str}) - {len(value['r'])}个回复")
        
        return results
    
    async def get_keyword_detail(self, group_id: str, user_id: str, keyword_id: int) -> Optional[Dict]:
        """获取关键词详情"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")
        
        if 1 <= keyword_id <= len(lexicon["work"]):
            item = lexicon["work"][keyword_id-1]
            key = list(item.keys())[0]
            return {
                "keyword": key,
                "responses": item[key]["r"],
                "mode": item[key]["s"]
            }
        
        return None

# ============================= AstrBot 插件主类 =============================
@register("keyword_astrbot", "Van", "关键词词库系统", "1.0.0")
class KeywordPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.keyword_manager = None
        self.admin_ids = set()
        self.ignore_groups = set()
        self.ignore_users = set()
        
    async def initialize(self):
        """初始化插件"""
        logger.info("关键词词库插件正在初始化...")
        
        # 解析配置
        self.parse_config()
        
        # 初始化词库管理器
        data_dir = self.config.get("data_directory", "data/keyword_astrbot")
        self.keyword_manager = KeywordManager(dict(self.config), data_dir)
        
        logger.info("关键词词库插件初始化完成")
    
    def parse_config(self):
        """解析配置项"""
        # 管理员ID
        admin_text = self.config.get("admin_ids", "")
        self.admin_ids = set(line.strip() for line in admin_text.split('\n') if line.strip())
        
        # 忽略的群
        ignore_groups_text = self.config.get("ignore_group_ids", "")
        self.ignore_groups = set(line.strip() for line in ignore_groups_text.split('\n') if line.strip())
        
        # 忽略的用户
        ignore_users_text = self.config.get("ignore_user_ids", "")
        self.ignore_users = set(line.strip() for line in ignore_users_text.split('\n') if line.strip())
    
    def is_admin(self, user_id: str) -> bool:
        """检查是否为管理员"""
        return user_id in self.admin_ids
    
    def should_ignore(self, group_id: str, user_id: str) -> bool:
        """检查是否应该忽略"""
        if group_id and group_id in self.ignore_groups:
            return True
        if user_id in self.ignore_users:
            return True
        return False
    
    # ============================= 消息处理 =============================
    
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_message(self, event: AstrMessageEvent):
        """处理群消息"""
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        
        # 检查是否忽略
        if self.should_ignore(group_id, user_id):
            return
        
        message_text = event.message_str.strip()
        
        # 检查是否为管理员指令
        is_admin = self.is_admin(user_id)
        if is_admin and await self.handle_admin_command(message_text, event):
            return
        
        # 普通关键词匹配
        result = await self.keyword_manager.search_keyword(
            message_text, 
            group_id, 
            user_id,
            is_admin
        )
        
        if result:
            # 检查冷却
            if "item_index" in result:
                cooling = await self.keyword_manager.check_cooling(
                    user_id, group_id, result["lexicon_id"], result["item_index"]
                )
                
                if isinstance(cooling, int):
                    # 还在冷却中
                    cooling_msg = f"冷却中，请等待 {cooling} 秒"
                    yield event.plain_result(cooling_msg)
                    return
            
            # 处理响应
            response_chain = await self.keyword_manager.process_response(result, None, event)
            
            if response_chain:
                yield event.chain_result(response_chain)
    
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_message(self, event: AstrMessageEvent):
        """处理私聊消息"""
        user_id = str(event.get_sender_id())
        
        # 检查是否为管理员
        if self.is_admin(user_id):
            message_text = event.message_str.strip()
            await self.handle_admin_command(message_text, event)
    
    async def handle_admin_command(self, message: str, event: AstrMessageEvent) -> bool:
        """处理管理员指令"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        
        # 精准问答指令
        if message.startswith("精准问答 "):
            parts = message[4:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_keyword(
                    group_id, user_id, keyword, response, 1
                )
                yield event.plain_result(msg)
                return True
        
        # 模糊问答指令
        elif message.startswith("模糊问答 "):
            parts = message[4:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_keyword(
                    group_id, user_id, keyword, response, 0
                )
                yield event.plain_result(msg)
                return True
        
        # 加选项指令
        elif message.startswith("加选项 "):
            parts = message[3:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_response(
                    group_id, user_id, keyword, response
                )
                yield event.plain_result(msg)
                return True
        
        # 删词指令
        elif message.startswith("删词 "):
            keyword = message[2:].strip()
            if keyword:
                success, msg = await self.keyword_manager.remove_keyword(
                    group_id, user_id, keyword
                )
                yield event.plain_result(msg)
                return True
        
        # 查词指令
        elif message.startswith("查词 "):
            keyword = message[2:].strip()
            keywords = await self.keyword_manager.list_keywords(
                group_id, user_id, keyword
            )
            
            if keywords:
                result = "关键词列表：\n" + "\n".join(keywords[:20])  # 限制显示数量
                if len(keywords) > 20:
                    result += f"\n...还有 {len(keywords)-20} 个词条"
            else:
                result = "未找到相关关键词"
            
            yield event.plain_result(result)
            return True
        
        # 清空词库指令
        elif message == "词库清空":
            if event.get_group_id():
                # 群聊中需要确认
                yield event.plain_result("请在私聊中使用此指令")
            else:
                lexicon_id = self.keyword_manager.get_lexicon_id(group_id, user_id)
                await self.keyword_manager.save_lexicon(lexicon_id, {"work": []})
                yield event.plain_result("词库已清空")
            return True
        
        # 备份词库指令
        elif message == "词库备份":
            # 这里可以添加备份到文件的功能
            yield event.plain_result("备份功能开发中...")
            return True
        
        # 切换词库指令
        elif message.startswith("切换词库 "):
            lexicon_name = message[5:].strip()
            if lexicon_name:
                # 更新选择配置
                self.keyword_manager.select_config[user_id] = lexicon_name
                select_path = os.path.join(self.keyword_manager.data_dir, "select.txt")
                lines = [f"{k}={v}" for k, v in self.keyword_manager.select_config.items()]
                async with aiofiles.open(select_path, 'w', encoding='utf-8') as f:
                    await f.write('\n'.join(lines))
                yield event.plain_result(f"已切换到词库: {lexicon_name}")
            return True
        
        return False
    
    # ============================= 指令系统 =============================
    
    @filter.command("keyword", alias={"关键词", "词库"})
    async def keyword_command(self, event: AstrMessageEvent):
        """关键词系统主指令"""
        yield event.plain_result(
            "关键词词库系统 v1.0\n\n"
            "可用指令：\n"
            "1. /keyword help - 查看帮助\n"
            "2. /keyword list - 列出关键词\n"
            "3. /keyword add - 添加关键词\n"
            "4. /keyword del - 删除关键词\n"
            "5. /keyword search - 搜索关键词"
        )
    
    @filter.command("keyword help")
    async def keyword_help(self, event: AstrMessageEvent):
        """查看详细帮助"""
        help_text = """📚 关键词词库系统使用说明

🔧 管理员指令（私聊或群聊中）：
1. 精准问答 关键词 回复内容
2. 模糊问答 关键词 回复内容
3. 加选项 关键词 新回复
4. 删词 关键词
5. 查词 关键词
6. 切换词库 词库名
7. 词库清空（私聊）
8. 词库备份

🎯 变量功能：
[qq] - 触发者QQ
[group] - 群号
[name] - 昵称
[id] - 消息ID
[n.1] - 通配符内容

🔄 特殊语法：
(1-100) - 随机数
(+1+2*3) - 计算
(3600~) - 冷却时间
{Y>10} - 条件判断

📷 媒体支持：
[图片.url]
[艾特.QQ号]
[表情.id]
[回复]"""
        
        yield event.plain_result(help_text)
    
    @filter.command("keyword list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_list(self, event: AstrMessageEvent):
        """列出关键词（仅管理员）"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        
        keywords = await self.keyword_manager.list_keywords(group_id, user_id)
        
        if keywords:
            result = "📋 关键词列表：\n" + "\n".join(keywords[:10])
            if len(keywords) > 10:
                result += f"\n...共 {len(keywords)} 个词条"
        else:
            result = "当前词库为空"
        
        yield event.plain_result(result)
    
    @filter.command_group("keyword")
    def keyword_group(self):
        """关键词指令组"""
        pass
    
    @keyword_group.command("add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_add(self, event: AstrMessageEvent, keyword: str, response: str):
        """添加关键词（仅管理员）"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        
        success, msg = await self.keyword_manager.add_keyword(
            group_id, user_id, keyword, response, 0
        )
        
        yield event.plain_result(msg)
    
    @keyword_group.command("delete")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_delete(self, event: AstrMessageEvent, keyword: str):
        """删除关键词（仅管理员）"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        
        success, msg = await self.keyword_manager.remove_keyword(
            group_id, user_id, keyword
        )
        
        yield event.plain_result(msg)
    
    async def terminate(self):
        """插件卸载时调用"""
        logger.info("关键词词库插件正在卸载...")