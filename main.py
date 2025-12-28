import os
import json
import re
import random
import asyncio
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from typing import Dict, List, Optional, Union, Any, Tuple
import aiofiles
import aiofiles.os as aos
from pathlib import Path

# 安全数学表达式求值库
try:
    from simpleeval import simple_eval, InvalidExpression
    SIMPLEEVAL_AVAILABLE = True
except ImportError:
    SIMPLEEVAL_AVAILABLE = False

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
from astrbot.api.message_components import *
from astrbot.api import AstrBotConfig


class SafeMathEvaluator:
    """安全的数学表达式求值器"""
    
    def __init__(self):
        self._cache = {}
        
    def evaluate(self, expr: str) -> Optional[Union[int, float]]:
        """安全地计算数学表达式"""
        if not expr:
            return None
            
        # 缓存结果
        if expr in self._cache:
            return self._cache[expr]
        
        # 清理表达式
        expr = expr.strip()
        
        # 只允许数字、基本运算符和括号
        safe_chars = set('0123456789+-*/.() ')
        if not all(c in safe_chars for c in expr):
            logger.warning(f"表达式包含不安全字符: {expr}")
            return None
        
        try:
            if SIMPLEEVAL_AVAILABLE:
                # 使用 simpleeval 进行安全求值
                result = simple_eval(expr)
            else:
                # 备用方案：仅支持基础四则运算
                result = self._basic_eval(expr)
            
            # 处理整数结果
            if isinstance(result, float) and result.is_integer():
                result = int(result)
            
            self._cache[expr] = result
            return result
            
        except (InvalidExpression, SyntaxError, ZeroDivisionError, ValueError) as e:
            logger.warning(f"表达式求值失败: {expr}, 错误: {e}")
            return None
    
    def _basic_eval(self, expr: str) -> Union[int, float]:
        """基础四则运算求值（备用方案）"""
        # 移除空格
        expr = expr.replace(' ', '')
        
        # 处理括号
        while '(' in expr:
            start = expr.rfind('(')
            end = expr.find(')', start)
            if end == -1:
                break
            sub_expr = expr[start+1:end]
            sub_result = self._basic_eval(sub_expr)
            expr = expr[:start] + str(sub_result) + expr[end+1:]
        
        # 处理乘除法
        operators = [('*', lambda a, b: a * b), 
                    ('/', lambda a, b: a / b if b != 0 else 0)]
        
        for op, func in operators:
            while op in expr:
                idx = expr.find(op)
                left = self._extract_left_number(expr, idx)
                right = self._extract_right_number(expr, idx)
                result = func(left, right)
                expr = expr[:idx-len(str(left))] + str(result) + expr[idx+len(str(right))+1:]
        
        # 处理加减法
        result = 0
        current_num = ''
        current_op = '+'
        
        for i, char in enumerate(expr):
            if char in '+-' or i == len(expr) - 1:
                if i == len(expr) - 1:
                    current_num += char
                
                if current_num:
                    num = float(current_num) if '.' in current_num else int(current_num)
                    if current_op == '+':
                        result += num
                    else:
                        result -= num
                
                if char in '+-':
                    current_op = char
                    current_num = ''
            else:
                current_num += char
        
        return result
    
    def _extract_left_number(self, expr: str, idx: int) -> Union[int, float]:
        """向左提取数字"""
        i = idx - 1
        num_str = ''
        
        while i >= 0 and expr[i] in '0123456789.':
            num_str = expr[i] + num_str
            i -= 1
        
        if '.' in num_str:
            return float(num_str)
        return int(num_str) if num_str else 0
    
    def _extract_right_number(self, expr: str, idx: int) -> Union[int, float]:
        """向右提取数字"""
        i = idx + 1
        num_str = ''
        
        while i < len(expr) and expr[i] in '0123456789.':
            num_str += expr[i]
            i += 1
        
        if '.' in num_str:
            return float(num_str)
        return int(num_str) if num_str else 0


class CoolingManager:
    """冷却时间管理器（避免文件竞态条件）"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._cooling_data: Dict[str, Dict[Tuple[str, int], float]] = {}
        self._lock = asyncio.Lock()
        self._dirty = False
        self._save_task = None
        
    async def check_cooling(self, user_id: str, lexicon_id: str, item_index: int) -> Union[bool, int]:
        """检查冷却时间"""
        key = (user_id, item_index)
        cooling_key = f"cooling_{lexicon_id}"
        
        # 确保内存中有数据
        if cooling_key not in self._cooling_data:
            await self._load_cooling_data(lexicon_id)
        
        async with self._lock:
            if cooling_key in self._cooling_data and key in self._cooling_data[cooling_key]:
                expire_time = self._cooling_data[cooling_key][key]
                current_time = time.time()
                
                if current_time >= expire_time:
                    # 冷却已结束，删除记录
                    del self._cooling_data[cooling_key][key]
                    self._dirty = True
                    return False  # 没有冷却
                else:
                    # 返回剩余秒数（整数）
                    remaining = int(expire_time - current_time)
                    return remaining if remaining > 0 else False
        
        return False  # 没有冷却记录
    
    async def set_cooling(self, user_id: str, lexicon_id: str, item_index: int, seconds: int):
        """设置冷却时间"""
        key = (user_id, item_index)
        cooling_key = f"cooling_{lexicon_id}"
        
        async with self._lock:
            if cooling_key not in self._cooling_data:
                self._cooling_data[cooling_key] = {}
            
            expire_time = time.time() + seconds
            self._cooling_data[cooling_key][key] = expire_time
            self._dirty = True
        
        # 触发异步保存
        await self._schedule_save(lexicon_id)
    
    async def _load_cooling_data(self, lexicon_id: str):
        """从文件加载冷却数据"""
        cooling_key = f"cooling_{lexicon_id}"
        
        cooling_path = self.data_dir / "cooling" / f"{lexicon_id}.json"
        
        if await aos.path.exists(cooling_path):
            try:
                async with aiofiles.open(cooling_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    data = json.loads(content)
                    
                    # 转换为内存格式
                    cooling_data = {}
                    for entry in data:
                        key = (entry["user_id"], entry["item_index"])
                        cooling_data[key] = entry["expire_time"]
                    
                    self._cooling_data[cooling_key] = cooling_data
                    
            except Exception as e:
                logger.error(f"加载冷却数据失败 {lexicon_id}: {e}")
                self._cooling_data[cooling_key] = {}
        else:
            self._cooling_data[cooling_key] = {}
    
    async def _schedule_save(self, lexicon_id: str):
        """计划保存数据（防抖）"""
        if self._save_task and not self._save_task.done():
            self._save_task.cancel()
        
        self._save_task = asyncio.create_task(self._save_cooling_data(lexicon_id))
    
    async def _save_cooling_data(self, lexicon_id: str):
        """保存冷却数据"""
        await asyncio.sleep(1)  # 防抖延迟
        
        async with self._lock:
            if not self._dirty:
                return
            
            cooling_key = f"cooling_{lexicon_id}"
            if cooling_key not in self._cooling_data:
                return
            
            # 过滤已过期的数据
            current_time = time.time()
            valid_data = {
                key: expire_time 
                for key, expire_time in self._cooling_data[cooling_key].items()
                if expire_time > current_time
            }
            self._cooling_data[cooling_key] = valid_data
            
            # 转换为可序列化格式
            save_data = [
                {
                    "user_id": user_id,
                    "item_index": item_index,
                    "expire_time": expire_time
                }
                for (user_id, item_index), expire_time in valid_data.items()
            ]
            
            # 保存到文件
            try:
                cooling_dir = self.data_dir / "cooling"
                await aos.makedirs(cooling_dir, exist_ok=True)
                cooling_path = cooling_dir / f"{lexicon_id}.json"
                
                async with aiofiles.open(cooling_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(save_data, indent=2, ensure_ascii=False))
                
                self._dirty = False
                logger.debug(f"冷却数据已保存: {lexicon_id}")
                
            except Exception as e:
                logger.error(f"保存冷却数据失败 {lexicon_id}: {e}")


class KeywordManager:
    def __init__(self, config: Dict):
        self.config = config
        
        # 使用 AstrBot 的标准插件数据目录
        self.data_dir = StarTools.get_data_dir()
        logger.info(f"Van词库数据目录: {self.data_dir}")
        
        # 检查目录是否存在，不存在则创建
        lexicon_dir = self.data_dir / "lexicon"
        if not lexicon_dir.exists():
            lexicon_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"创建词库目录: {lexicon_dir}")
        
        self.lexicons: Dict[str, Dict] = {}
        self.switch_config: Dict[str, str] = {}
        self.select_config: Dict[str, str] = {}
        self.math_evaluator = SafeMathEvaluator()
        self.cooling_manager = CoolingManager(self.data_dir)
        
        # 内置词库内容
        self.builtin_keywords = [
            {
                "keyword": "喵撒娇",
                "responses": [
                    "「尾巴绕你手腕~不许躲！@[qq]+(10-50)好感」",
                    "「蹭蹭你~要抱抱！(10-50)好感度增加！」",
                    "「喵~最喜欢你了！@[qq] 获得了(20-60)好感」",
                    "「用脑袋顶你手心~不许停！@[qq]+(15-45)好感度UP」"
                ],
                "mode": 0,  # 模糊匹配
                "description": "喵系撒娇互动"
            },
            {
                "keyword": "早安",
                "responses": [
                    "「早安~今天也要加油哦！(1-100)点能量注入！」",
                    "「早上好！为你准备了(1-3)杯咖啡~」",
                    "「新的一天开始啦！获得(5-20)点活力值」",
                    "「晨光中向你问好~今天幸运值是(1-100)」"
                ],
                "mode": 1,  # 精确匹配
                "description": "早安问候"
            },
            {
                "keyword": "晚安",
                "responses": [
                    "「晚安~祝你好梦！回复(1-50)点体力」",
                    "「月色真美，做个好梦吧~获得(5-30)点睡眠质量」",
                    "「闭上眼睛数羊吧~已经帮你数了(10-100)只」",
                    "「晚安[name]，明天见！(1-80)点温馨度get」"
                ],
                "mode": 1,  # 精确匹配
                "description": "晚安问候"
            },
            {
                "keyword": "今天天气如何",
                "responses": [
                    "「让我看看~今天适合增加(10-60)点外出心情」",
                    "「天气不错呢！建议进行(1-5)小时户外活动」",
                    "「多云转晴~幸运加成(1-30)%」",
                    "「今日宜：[n.1] 忌：[n.2]」"
                ],
                "mode": 0,  # 模糊匹配
                "description": "天气查询"
            },
            {
                "keyword": "摸头",
                "responses": [
                    "「唔...摸头杀！@[qq]获得(15-45)点亲密度」",
                    "「被摸头了~开心！(20-60)点愉悦值上升」",
                    "「摸摸~像这样？@[qq] 被返还了(10-40)点摸头能量」",
                    "「头被摸得暖洋洋的~回赠你(5-25)点温暖值」"
                ],
                "mode": 0,  # 模糊匹配
                "description": "摸头互动"
            },
            {
                "keyword": "抽卡",
                "responses": [
                    "「卡池转动中...获得了(1-5)星角色！」",
                    "「单抽出奇迹！(1-100)幸运值判定中...」",
                    "「十连保底启动！@[qq]获得了(1-10)张SSR」",
                    "「今日运势：(1-100)，适合抽卡概率：(+运势*0.8)%」"
                ],
                "mode": 0,  # 模糊匹配
                "description": "抽卡模拟"
            },
            {
                "keyword": "[n.1]多少好感",
                "responses": [
                    "「查询中...@[n.1]当前好感度：(10-1000)」",
                    "「[n.1]对你的好感是：(50-500)，要继续加油哦！」",
                    "「秘密数据读取：[n.1]→[qq]：好感值(1-999)」",
                    "「好感雷达显示：[n.1]对你的好感为(100-300)点」"
                ],
                "mode": 0,  # 模糊匹配
                "description": "好感度查询（通配符）"
            }
        ]
        
    async def initialize(self):
        """异步初始化"""
        logger.info("Van词库插件正在初始化...")
        
        # 确保目录存在
        await self._ensure_directories()
        
        # 异步加载配置
        await self.load_configs()
        
        # 创建内置词库
        await self._create_builtin_lexicon()
        
        logger.info("Van词库插件初始化完成")
        
    async def _ensure_directories(self):
        """确保必要的目录存在"""
        dirs = [
            self.data_dir / "lexicon",
            self.data_dir / "config",
            self.data_dir / "cooling",
            self.data_dir / "backups",
            self.data_dir / "filecache"
        ]
        
        for dir_path in dirs:
            try:
                await aos.makedirs(dir_path, exist_ok=True)
                logger.debug(f"确保目录存在: {dir_path}")
            except Exception as e:
                logger.error(f"创建目录失败 {dir_path}: {e}")
    
    async def load_configs(self):
        """异步加载配置文件"""
        # 加载开关配置
        switch_path = self.data_dir / "switch.txt"
        if await aos.path.exists(switch_path):
            try:
                async with aiofiles.open(switch_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    for line in content.splitlines():
                        line = line.strip()
                        if line and '=' in line:
                            k, v = line.split('=', 1)
                            self.switch_config[k.strip()] = v.strip()
                logger.info(f"加载开关配置: {len(self.switch_config)} 条")
            except Exception as e:
                logger.error(f"加载开关配置失败: {e}")
        
        # 加载选择配置
        select_path = self.data_dir / "select.txt"
        if await aos.path.exists(select_path):
            try:
                async with aiofiles.open(select_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    for line in content.splitlines():
                        line = line.strip()
                        if line and '=' in line:
                            k, v = line.split('=', 1)
                            self.select_config[k.strip()] = v.strip()
                logger.info(f"加载选择配置: {len(self.select_config)} 条")
            except Exception as e:
                logger.error(f"加载选择配置失败: {e}")
    
    async def _create_builtin_lexicon(self):
        """创建内置词库"""
        builtin_lexicon_id = "builtin_default"
        builtin_path = self.data_dir / "lexicon" / f"{builtin_lexicon_id}.json"
        
        # 如果内置词库已存在，检查是否需要更新
        if await aos.path.exists(builtin_path):
            try:
                async with aiofiles.open(builtin_path, 'r', encoding='utf-8') as f:
                    existing_data = json.loads(await f.read())
                
                # 检查版本标识
                if existing_data.get("_metadata", {}).get("version") == "1.0":
                    logger.info("内置词库已存在且为最新版本")
                    return
            except Exception as e:
                logger.warning(f"读取现有内置词库失败，将重新创建: {e}")
        
        # 构建内置词库数据结构
        builtin_lexicon = {
            "_metadata": {
                "name": "内置默认词库",
                "version": "1.0",
                "created_at": datetime.now().isoformat(),
                "description": "Van词库系统内置词库",
                "keyword_count": len(self.builtin_keywords)
            },
            "work": []
        }
        
        # 添加内置关键词
        for item in self.builtin_keywords:
            keyword_item = {
                item["keyword"]: {
                    "r": item["responses"],
                    "s": item["mode"]
                }
            }
            builtin_lexicon["work"].append(keyword_item)
        
        # 保存内置词库
        try:
            async with aiofiles.open(builtin_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(builtin_lexicon, indent=4, ensure_ascii=False))
            logger.info(f"内置词库创建成功: {builtin_lexicon_id}")
            
        except Exception as e:
            logger.error(f"创建内置词库失败: {e}")
    
    def get_lexicon_id(self, group_id: str, user_id: str = "") -> str:
        """
        获取词库ID
        逻辑：优先使用用户选择的词库，然后使用群组开关配置的词库，最后使用默认词库
        """
        # 1. 用户选择的词库（通过select.txt配置）
        if user_id and user_id in self.select_config:
            lexicon_id = self.select_config[user_id]
            logger.debug(f"使用用户选择词库: user={user_id}, lexicon={lexicon_id}")
            return lexicon_id
        
        # 2. 群组开关配置的词库（通过switch.txt配置）
        if group_id and group_id in self.switch_config:
            lexicon_id = self.switch_config[group_id]
            if lexicon_id:  # 非空字符串
                logger.debug(f"使用群组开关词库: group={group_id}, lexicon={lexicon_id}")
                return lexicon_id
        
        # 3. 默认词库（私聊使用用户ID，群聊使用群组ID）
        if not group_id or group_id == "":
            # 私聊：使用用户ID作为词库ID
            lexicon_id = f"private_{user_id}"
        else:
            # 群聊：使用群组ID作为词库ID
            lexicon_id = group_id
        
        logger.debug(f"使用默认词库: group={group_id}, user={user_id}, lexicon={lexicon_id}")
        return lexicon_id
    
    async def get_lexicon(self, lexicon_id: str, user_id: str = "") -> Dict:
        """获取词库数据"""
        lexicon_path = self.data_dir / "lexicon" / f"{lexicon_id}.json"

        # 内存缓存
        if lexicon_id in self.lexicons:
            return self.lexicons[lexicon_id]

        try:
            if await aos.path.exists(lexicon_path):
                logger.info(f"从文件加载词库: {lexicon_path}")
                async with aiofiles.open(lexicon_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
                    data = json.loads(content)
                    self.lexicons[lexicon_id] = data
                    
                    # 记录词库信息
                    word_count = len(data.get("work", []))
                    logger.info(f"词库 {lexicon_id} 加载成功，包含 {word_count} 个词条")
                    return data
            else:
                logger.info(f"词库文件不存在，创建空词库: {lexicon_path}")
                # 创建空词库文件
                empty_data = {"work": []}
                async with aiofiles.open(lexicon_path, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(empty_data, indent=4, ensure_ascii=False))
                
                self.lexicons[lexicon_id] = empty_data
                return empty_data
                
        except Exception as e:
            logger.error(f"加载词库失败 {lexicon_id}: {e}")
            # 返回空词库
            empty_data = {"work": []}
            self.lexicons[lexicon_id] = empty_data
            return empty_data

    async def save_lexicon(self, lexicon_id: str, data: Dict):
        """保存词库"""
        lexicon_path = self.data_dir / "lexicon" / f"{lexicon_id}.json"
        self.lexicons[lexicon_id] = data

        try:
            async with aiofiles.open(lexicon_path, 'w', encoding='utf-8') as f:
                await f.write(json.dumps(data, indent=4, ensure_ascii=False))
            logger.info(f"词库保存成功: {lexicon_id}, 词条数: {len(data.get('work', []))}")
        except Exception as e:
            logger.error(f"保存词库失败 {lexicon_id}: {e}")

    async def search_keyword(self, text: str, group_id: str, user_id: str, is_admin: bool = False) -> Optional[Dict]:
        """搜索匹配的关键词"""
        current_lexicon_id = self.get_lexicon_id(group_id, user_id)
        
        # 搜索顺序：内置词库 -> 当前词库 -> 默认词库
        lexicon_ids = ["builtin_default", current_lexicon_id]
        
        # 如果是群聊，并且当前不是使用的群组默认词库，则也搜索群组默认词库
        if group_id and current_lexicon_id != group_id:
            lexicon_ids.append(group_id)
        
        # 如果是私聊，并且当前不是使用的用户默认词库，则也搜索用户默认词库
        if not group_id and current_lexicon_id != f"private_{user_id}":
            lexicon_ids.append(f"private_{user_id}")

        logger.debug(f"搜索关键词: text='{text}', group={group_id}, user={user_id}")
        logger.debug(f"搜索词库列表: {lexicon_ids}")

        for lid in lexicon_ids:
            try:
                lex_data = await self.get_lexicon(lid, "")
                logger.debug(f"检查词库 {lid}: 词条数={len(lex_data.get('work', []))}")
                
                for idx, item in enumerate(lex_data.get("work", [])):
                    for key, value in item.items():
                        # 检查管理员模式
                        if value.get("s") == 10 and not is_admin:
                            logger.debug(f"跳过管理员模式词条: {key}")
                            continue
                        
                        # 检查通配符匹配
                        if "[n." in key:
                            match_result = self.match_wildcard(key, text)
                            if match_result:
                                logger.info(f"通配符匹配成功: 词库={lid}, key='{key}', text='{text}'")
                                return {
                                    "type": "wildcard",
                                    "response": random.choice(value["r"]),
                                    "matches": match_result,
                                    "lexicon_id": lid,
                                    "item_index": idx,
                                    "keyword": key
                                }
                        
                        # 精确匹配
                        if value.get("s") == 1 and key == text:
                            logger.info(f"精确匹配成功: 词库={lid}, key='{key}', text='{text}'")
                            return {
                                "type": "exact",
                                "response": random.choice(value["r"]),
                                "lexicon_id": lid,
                                "item_index": idx,
                                "keyword": key
                            }
                        
                        # 模糊匹配
                        if value.get("s") == 0 and key in text:
                            logger.info(f"模糊匹配成功: 词库={lid}, key='{key}', text='{text}'")
                            return {
                                "type": "fuzzy",
                                "response": random.choice(value["r"]),
                                "lexicon_id": lid,
                                "item_index": idx,
                                "keyword": key
                            }
            except Exception as e:
                logger.warning(f"搜索词库 {lid} 时出错: {e}")
                continue
        
        logger.debug(f"未找到匹配的关键词: '{text}'")
        return None

    def match_wildcard(self, pattern: str, text: str) -> Optional[List[str]]:
        """通配符匹配"""
        # 转义特殊字符
        safe_pattern = re.escape(pattern)
        # 将 [n.x] 替换为 (.+?)
        safe_pattern = re.sub(r'\\\[n\\.(\d+)\\\]', r'(.+?)', safe_pattern)

        try:
            match = re.match(f"^{safe_pattern}$", text)
            if match:
                groups = match.groups()
                result = ["", "", "", "", "", ""]  # n.0 到 n.5
                
                # 获取所有占位符索引
                placeholders = re.findall(r'\[n\.(\d+)\]', pattern)
                for idx, ph in enumerate(placeholders):
                    ph_idx = int(ph)
                    if ph_idx < len(result) and idx < len(groups):
                        result[ph_idx] = groups[idx]
                return result
        except re.error as e:
            logger.error(f"正则匹配错误: {e}")

        return None

    async def process_response(self, response: str, matches: Optional[List[str]], event: AstrMessageEvent) -> Optional[List[BaseMessageComponent]]:
        """处理响应文本，返回消息组件列表"""
        if isinstance(response, dict):
            base_response = response["response"]
            matches = response.get("matches", [])
        else:
            base_response = response
            matches = matches or []

        text = base_response

        # 替换通配符
        if matches:
            for i in range(1, 6):
                if i < len(matches) and matches[i]:
                    text = text.replace(f"[n.{i}]", matches[i])
                    # 清理通配符内容，只保留安全字符
                    clean_match = re.search(r'[\d\w/.:?=&-]+', matches[i])
                    if clean_match:
                        text = text.replace(f"[n.{i}.t]", clean_match.group())

        # 获取发送者信息 - 使用AstrBot标准API
        group_id = event.get_group_id() or ""
        sender_id = str(event.get_sender_id())
        
        # 使用 event.get_sender_name() 获取发送者名称
        sender_name = event.get_sender_name() or sender_id
        
        # 替换用户变量
        text = text.replace("[qq]", sender_id)
        text = text.replace("[group]", str(group_id))
        text = text.replace("[name]", sender_name)
        text = text.replace("[card]", sender_name)
        
        # 获取机器人ID
        try:
            bot_id = event.self_id  # 通用属性
            text = text.replace("[ai]", str(bot_id))
        except AttributeError:
            # 备选方法
            try:
                bot_id = event.bot_id if hasattr(event, 'bot_id') else "unknown"
                text = text.replace("[ai]", str(bot_id))
            except:
                text = text.replace("[ai]", "unknown")

        # 消息ID - 使用 message_obj 属性
        try:
            message_id = str(event.message_obj.message_id)
            text = text.replace("[id]", message_id)
            text = text.replace("[消息id]", message_id)
        except AttributeError:
            logger.warning("无法获取消息ID，跳过 [id] 和 [消息id] 变量替换")

        # 处理随机数
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

        # 安全处理计算表达式
        while True:
            match = re.search(r'\(\+([^\)]+)\)', text)
            if not match:
                break
            expr = match.group(1)
            try:
                # 使用安全求值器
                result = self.math_evaluator.evaluate(expr)
                if result is not None:
                    text = text.replace(match.group(0), str(result), 1)
                else:
                    # 求值失败，保留原表达式
                    break
            except Exception as e:
                logger.error(f"数学表达式求值异常: {expr}, 错误: {e}")
                break

        # 处理条件判断
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
            except ValueError:
                result = False

            if result:
                text = re.sub(r'\{.*?[><=].*?\}', '', text)
            else:
                return None

        # 解析特殊命令
        return await self.parse_special_commands(text, event)

    async def parse_special_commands(self, text: str, event: AstrMessageEvent) -> List[BaseMessageComponent]:
        """解析特殊命令，返回消息组件列表"""
        chain = []

        parts = re.split(r'(\[.*?\])', text)

        for part in parts:
            if not part.strip():
                continue

            if part.startswith('[') and part.endswith(']'):
                cmd = part[1:-1]
                cmd_parts = cmd.split('.')

                if len(cmd_parts) >= 2:
                    cmd_type = cmd_parts[0].lower()

                    if cmd_type in ["image", "图片"]:
                        url = '.'.join(cmd_parts[1:])
                        if url.startswith(('http://', 'https://')):
                            try:
                                chain.append(Image.fromURL(url))
                            except Exception as e:
                                logger.error(f"加载图片失败: {url}, 错误: {e}")
                                chain.append(Plain(f"[图片加载失败: {url}]"))
                        else:
                            try:
                                # 相对于插件数据目录的文件
                                file_path = self.data_dir / "filecache" / url
                                chain.append(Image.fromFileSystem(str(file_path)))
                            except Exception as e:
                                logger.error(f"加载本地图片失败: {url}, 错误: {e}")
                                chain.append(Plain(f"[本地图片加载失败: {url}]"))

                    elif cmd_type in ["at", "艾特"]:
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            qq = cmd_parts[1]
                            chain.append(At(qq=qq))
                        else:
                            chain.append(At(qq=str(event.get_sender_id())))

                    elif cmd_type in ["face", "表情"]:
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            face_id = cmd_parts[1]
                            chain.append(Face(id=face_id))

                    elif cmd_type in ["reply", "回复"]:
                        if len(cmd_parts) >= 2 and cmd_parts[1]:
                            msg_id = cmd_parts[1]
                            chain.append(Reply(message_id=msg_id))
                        else:
                            # 使用 event.message_obj 获取消息ID
                            try:
                                msg_id = event.message_obj.message_id
                                chain.append(Reply(message_id=msg_id))
                            except AttributeError:
                                logger.warning("无法获取消息ID，跳过回复消息段")
                                chain.append(Plain("[回复]"))

                    elif cmd_type in ["record", "语音"]:
                        url = '.'.join(cmd_parts[1:])
                        try:
                            chain.append(Record(file=url))
                        except Exception as e:
                            logger.error(f"加载语音失败: {url}, 错误: {e}")
                            chain.append(Plain(f"[语音加载失败: {url}]"))

                    elif cmd_type == "poke":
                        if len(cmd_parts) >= 3:
                            target_id = cmd_parts[1]
                            chain.append(Poke(qq=target_id))

                    else:
                        chain.append(Plain(part))
            else:
                chain.append(Plain(part))

        return chain

    # 管理功能
    async def add_keyword(self, group_id: str, user_id: str, keyword: str, response: str, mode: int = 0) -> Tuple[bool, str]:
        """添加关键词"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")

        # 检查词条是否已存在
        for item in lexicon["work"]:
            if keyword in item:
                return False, "词条已存在"

        # 容错处理
        if self.config.get("mistake_turn_type", False):
            keyword = (keyword.replace('【', '[').replace('】', ']')
                      .replace('（', '(').replace('）', ')')
                      .replace('｛', '{').replace('｝', '}').replace('：', ':'))

        new_item = {keyword: {"r": [response], "s": mode}}
        lexicon["work"].append(new_item)

        await self.save_lexicon(lexicon_id, lexicon)
        return True, "添加成功"

    async def remove_keyword(self, group_id: str, user_id: str, keyword: str) -> Tuple[bool, str]:
        """删除关键词"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")

        new_work = [item for item in lexicon["work"] if keyword not in item]

        if len(new_work) == len(lexicon["work"]):
            return False, "词条不存在"

        lexicon["work"] = new_work
        await self.save_lexicon(lexicon_id, lexicon)
        return True, "删除成功"

    async def add_response(self, group_id: str, user_id: str, keyword: str, response: str) -> Tuple[bool, str]:
        """为关键词添加回复选项"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")

        for item in lexicon["work"]:
            if keyword in item:
                item[keyword]["r"].append(response)
                await self.save_lexicon(lexicon_id, lexicon)
                return True, "添加成功"

        return False, "词条不存在"

    async def remove_response(self, group_id: str, user_id: str, keyword: str, response: str) -> Tuple[bool, str]:
        """删除关键词的某个回复选项"""
        lexicon_id = self.get_lexicon_id(group_id, user_id)
        lexicon = await self.get_lexicon(lexicon_id, "")

        for item in lexicon["work"]:
            if keyword in item and response in item[keyword]["r"]:
                item[keyword]["r"].remove(response)
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


@register("keyword_astrbot", "Van", "Van词库系统", "1.0.0")
class KeywordPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.keyword_manager = None
        self.admin_ids = set()
        self.ignore_groups = set()
        self.ignore_users = set()

    async def initialize(self):
        logger.info("Van词库插件正在初始化...")

        self.parse_config()

        self.keyword_manager = KeywordManager(dict(self.config))
        await self.keyword_manager.initialize()

        logger.info("Van词库插件初始化完成")

    def parse_config(self):
        """解析配置"""
        admin_text = self.config.get("admin_ids", "")
        self.admin_ids = set(line.strip() for line in admin_text.split('\n') if line.strip())

        ignore_groups_text = self.config.get("ignore_group_ids", "")
        self.ignore_groups = set(line.strip() for line in ignore_groups_text.split('\n') if line.strip())

        ignore_users_text = self.config.get("ignore_user_ids", "")
        self.ignore_users = set(line.strip() for line in ignore_users_text.split('\n') if line.strip())
        
        logger.info(f"管理员列表: {self.admin_ids}")
        logger.info(f"忽略群组: {self.ignore_groups}")
        logger.info(f"忽略用户: {self.ignore_users}")

    def is_admin(self, user_id: str) -> bool:
        """检查是否为管理员"""
        return user_id in self.admin_ids

    def should_ignore(self, group_id: str, user_id: str) -> bool:
        """检查是否应该忽略"""
        if group_id and group_id in self.ignore_groups:
            logger.debug(f"忽略群组消息: group={group_id}")
            return True
        if user_id in self.ignore_users:
            logger.debug(f"忽略用户消息: user={user_id}")
            return True
        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_group_message(self, event: AstrMessageEvent):
        """处理群聊消息"""
        # 过滤自身消息
        try:
            bot_id = event.self_id  # 通用属性
            sender_id = event.get_sender_id()
            if str(sender_id) == str(bot_id):
                logger.debug(f"忽略自身消息: sender_id={sender_id}, bot_id={bot_id}")
                return
        except AttributeError:
            # 如果 event 没有 self_id 属性，尝试其他方法
            try:
                bot_id = event.bot_id if hasattr(event, 'bot_id') else None
                sender_id = event.get_sender_id()
                if bot_id and str(sender_id) == str(bot_id):
                    logger.debug(f"忽略自身消息 (备用方法): sender_id={sender_id}, bot_id={bot_id}")
                    return
            except:
                pass  # 如果无法获取，继续处理
        
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())

        logger.debug(f"收到群聊消息: group={group_id}, user={user_id}")

        if self.should_ignore(group_id, user_id):
            return

        message_text = event.message_str.strip()

        # 先检查是否为管理员指令
        if self.is_admin(user_id):
            handled = await self.handle_admin_command(message_text, event)
            if handled:
                return

        # 关键词匹配
        result = await self.keyword_manager.search_keyword(
            message_text,
            group_id,
            user_id,
            self.is_admin(user_id)
        )

        if result:
            logger.info(f"关键词匹配成功: {result.get('keyword')}")
            
            # 检查冷却时间
            lexicon_id = result.get("lexicon_id", "")
            item_index = result.get("item_index", 0)
            
            cooling = await self.keyword_manager.cooling_manager.check_cooling(
                user_id, lexicon_id, item_index
            )

            # cooling 为 False 表示没有冷却，为 int 表示剩余秒数
            if isinstance(cooling, int) and cooling > 0:
                cooling_msg = f"冷却中，请等待 {cooling} 秒"
                logger.debug(f"触发冷却: {cooling_msg}")
                yield event.plain_result(cooling_msg)
                return

            # 处理响应
            response_chain = await self.keyword_manager.process_response(result, None, event)

            if response_chain:
                logger.debug(f"发送响应消息，组件数: {len(response_chain)}")
                yield event.chain_result(response_chain)
                
                # 处理冷却时间设置
                cooling_match = re.search(r'\((\d+)~\)', result.get("response", ""))
                if cooling_match:
                    seconds = int(cooling_match.group(1))
                    if seconds == 0:
                        tomorrow = datetime.now() + timedelta(days=1)
                        tomorrow_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
                        seconds = int(tomorrow_midnight.timestamp() - datetime.now().timestamp())
                    
                    await self.keyword_manager.cooling_manager.set_cooling(
                        user_id, lexicon_id, item_index, seconds
                    )
                    logger.debug(f"设置冷却时间: {seconds}秒")

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_message(self, event: AstrMessageEvent):
        """处理私聊消息"""
        # 过滤自身消息
        try:
            bot_id = event.self_id  # 通用属性
            sender_id = event.get_sender_id()
            if str(sender_id) == str(bot_id):
                logger.debug(f"忽略自身消息: sender_id={sender_id}, bot_id={bot_id}")
                return
        except AttributeError:
            # 如果 event 没有 self_id 属性，尝试其他方法
            try:
                bot_id = event.bot_id if hasattr(event, 'bot_id') else None
                sender_id = event.get_sender_id()
                if bot_id and str(sender_id) == str(bot_id):
                    logger.debug(f"忽略自身消息 (备用方法): sender_id={sender_id}, bot_id={bot_id}")
                    return
            except:
                pass  # 如果无法获取，继续处理
        
        user_id = str(event.get_sender_id())
        logger.debug(f"收到私聊消息: user={user_id}")

        if self.should_ignore("", user_id):
            return

        message_text = event.message_str.strip()

        # 私聊也支持管理员指令
        if self.is_admin(user_id):
            handled = await self.handle_admin_command(message_text, event)
            if handled:
                return

        # 私聊关键词匹配
        result = await self.keyword_manager.search_keyword(
            message_text,
            "",  # 私聊没有群组ID
            user_id,
            self.is_admin(user_id)
        )

        if result:
            logger.info(f"私聊关键词匹配成功: {result.get('keyword')}")
            
            # 检查冷却时间
            lexicon_id = result.get("lexicon_id", "")
            item_index = result.get("item_index", 0)
            
            cooling = await self.keyword_manager.cooling_manager.check_cooling(
                user_id, lexicon_id, item_index
            )

            # cooling 为 False 表示没有冷却，为 int 表示剩余秒数
            if isinstance(cooling, int) and cooling > 0:
                cooling_msg = f"冷却中，请等待 {cooling} 秒"
                logger.debug(f"私聊触发冷却: {cooling_msg}")
                yield event.plain_result(cooling_msg)
                return

            # 处理响应
            response_chain = await self.keyword_manager.process_response(result, None, event)

            if response_chain:
                logger.debug(f"发送私聊响应消息，组件数: {len(response_chain)}")
                yield event.chain_result(response_chain)
                
                # 处理冷却时间设置
                cooling_match = re.search(r'\((\d+)~\)', result.get("response", ""))
                if cooling_match:
                    seconds = int(cooling_match.group(1))
                    if seconds == 0:
                        tomorrow = datetime.now() + timedelta(days=1)
                        tomorrow_midnight = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
                        seconds = int(tomorrow_midnight.timestamp() - datetime.now().timestamp())
                
                    await self.keyword_manager.cooling_manager.set_cooling(
                        user_id, lexicon_id, item_index, seconds
                    )
                    logger.debug(f"设置私聊冷却时间: {seconds}秒")

    async def handle_admin_command(self, message: str, event: AstrMessageEvent) -> bool:
        """处理管理员指令，返回是否处理成功"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())

        logger.debug(f"检查管理员指令: {message}")

        # 精准问答
        if message.startswith("精准问答 "):
            parts = message[4:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_keyword(
                    group_id, user_id, keyword, response, 1
                )
                await event.send(event.plain_result(msg))
                return True

        # 模糊问答
        elif message.startswith("模糊问答 "):
            parts = message[4:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_keyword(
                    group_id, user_id, keyword, response, 0
                )
                await event.send(event.plain_result(msg))
                return True

        # 加选项
        elif message.startswith("加选项 "):
            parts = message[3:].strip().split(maxsplit=2)
            if len(parts) >= 2:
                keyword = parts[0]
                response = parts[1]
                success, msg = await self.keyword_manager.add_response(
                    group_id, user_id, keyword, response
                )
                await event.send(event.plain_result(msg))
                return True

        # 删词
        elif message.startswith("删词 "):
            keyword = message[2:].strip()
            if keyword:
                success, msg = await self.keyword_manager.remove_keyword(
                    group_id, user_id, keyword
                )
                await event.send(event.plain_result(msg))
                return True

        # 查词
        elif message.startswith("查词 "):
            keyword = message[2:].strip()
            keywords = await self.keyword_manager.list_keywords(
                group_id, user_id, keyword
            )

            if keywords:
                result = "关键词列表：\n" + "\n".join(keywords[:20])
                if len(keywords) > 20:
                    result += f"\n...还有 {len(keywords)-20} 个词条"
            else:
                result = "未找到相关关键词"

            await event.send(event.plain_result(result))
            return True

        # 词库清空（私聊使用）
        elif message == "词库清空":
            lexicon_id = self.keyword_manager.get_lexicon_id(group_id, user_id)
            await self.keyword_manager.save_lexicon(lexicon_id, {"work": []})
            await event.send(event.plain_result("词库已清空"))
            return True

        # 词库备份
        elif message == "词库备份":
            lexicon_id = self.keyword_manager.get_lexicon_id(group_id, user_id)
            lexicon_path = StarTools.get_data_dir() / "lexicon" / f"{lexicon_id}.json"
            
            if await aos.path.exists(lexicon_path):
                backup_dir = StarTools.get_data_dir() / "backups"
                await aos.makedirs(backup_dir, exist_ok=True)
                
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = backup_dir / f"{lexicon_id}_{timestamp}.json"
                
                try:
                    async with aiofiles.open(lexicon_path, 'r', encoding='utf-8') as src:
                        async with aiofiles.open(backup_path, 'w', encoding='utf-8') as dst:
                            await dst.write(await src.read())
                    
                    await event.send(event.plain_result(f"词库备份成功：{backup_path.name}"))
                except Exception as e:
                    logger.error(f"备份词库失败: {e}")
                    await event.send(event.plain_result("备份失败，请查看日志"))
            else:
                await event.send(event.plain_result("词库文件不存在"))
            return True

        # 切换词库
        elif message.startswith("切换词库 "):
            lexicon_name = message[5:].strip()
            if lexicon_name:
                self.keyword_manager.select_config[user_id] = lexicon_name
                select_path = StarTools.get_data_dir() / "select.txt"
                lines = [f"{k}={v}" for k, v in self.keyword_manager.select_config.items()]
                try:
                    async with aiofiles.open(select_path, 'w', encoding='utf-8') as f:
                        await f.write('\n'.join(lines))
                    await event.send(event.plain_result(f"已切换到词库: {lexicon_name}"))
                except Exception as e:
                    logger.error(f"保存选择配置失败: {e}")
                    await event.send(event.plain_result("切换失败，请查看日志"))
            return True

        return False

    # 以下是命令组定义


# 修改你的命令组定义如下：

@filter.command_group("keyword", alias={"关键词", "词库"})
def keyword_group(self):
    pass

# 使用 __group_main__ 作为特殊名称
@keyword_group.command("helps")
async def keyword_main(self, event: AstrMessageEvent):
    """处理 /keyword 命令（显示帮助）"""
    yield event.plain_result(
        "Van词库系统 v1.0\n\n"
        "可用指令：\n"
        "1. /keyword help - 查看帮助\n"
        "2. /keyword list - 列出关键词\n"
        "3. /keyword add <关键词> <回复> - 添加关键词\n"
        "4. /keyword delete <关键词> - 删除关键词\n"
        "5. /keyword search <关键词> - 搜索关键词\n"
        "6. /keyword backup - 备份当前词库\n"
        "7. /keyword builtin - 内置词库管理\n\n"
        "🔧 管理员也可使用快捷指令：\n"
        "• 精准问答 关键词 回复\n"
        "• 模糊问答 关键词 回复\n"
        "• 加选项 关键词 回复\n"
        "• 删词 关键词\n"
        "• 查词 关键词"
    )

    @keyword_group.command("help")
    async def keyword_help(self, event: AstrMessageEvent):
        help_text = """📚 Van词库系统使用说明

🔧 管理员指令（私聊或群聊中）：
1. 精准问答 关键词 回复内容
2. 模糊问答 关键词 回复内容
3. 加选项 关键词 新回复
4. 删词 关键词
5. 查词 关键词
6. 切换词库 词库名
7. 词库清空（私聊）
8. 词库备份

🎮 普通用户指令：
1. /keyword help - 查看帮助
2. /keyword list - 列出关键词
3. /keyword search <关键词> - 搜索关键词

🎯 变量功能：
[qq] - 触发者QQ
[group] - 群号（私聊为空）
[name] - 昵称
[id] - 消息ID
[n.1] - 通配符内容

🔄 安全语法：
(1-100) - 随机数
(+1+2*3) - 安全计算
(3600~) - 冷却时间
{Y>10} - 条件判断

📷 媒体支持：
[图片.url]
[艾特.QQ号]
[表情.id]
[回复]

💡 提示：管理员在插件配置中添加QQ号后可使用管理员指令"""

        yield event.plain_result(help_text)

    @keyword_group.command("list")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_list(self, event: AstrMessageEvent):
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

    @keyword_group.command("add")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_add(self, event: AstrMessageEvent, keyword: str, response: str):
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())

        success, msg = await self.keyword_manager.add_keyword(
            group_id, user_id, keyword, response, 0
        )

        yield event.plain_result(msg)

    @keyword_group.command("delete")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_delete(self, event: AstrMessageEvent, keyword: str):
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())

        success, msg = await self.keyword_manager.remove_keyword(
            group_id, user_id, keyword
        )

        yield event.plain_result(msg)

    @keyword_group.command("backup")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_backup(self, event: AstrMessageEvent):
        """备份当前词库"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        lexicon_id = self.keyword_manager.get_lexicon_id(group_id, user_id)
        
        lexicon_path = StarTools.get_data_dir() / "lexicon" / f"{lexicon_id}.json"
        
        if await aos.path.exists(lexicon_path):
            backup_dir = StarTools.get_data_dir() / "backups"
            await aos.makedirs(backup_dir, exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"{lexicon_id}_{timestamp}.json"
            
            try:
                async with aiofiles.open(lexicon_path, 'r', encoding='utf-8') as src:
                    async with aiofiles.open(backup_path, 'w', encoding='utf-8') as dst:
                        await dst.write(await src.read())
                
                file_size = (await aos.stat(backup_path)).st_size
                yield event.plain_result(f"✅ 备份成功！\n文件名: {backup_path.name}\n大小: {file_size} 字节")
            except Exception as e:
                logger.error(f"备份词库失败: {e}")
                yield event.plain_result("❌ 备份失败，请查看日志")
        else:
            yield event.plain_result("❌ 词库文件不存在")

    @keyword_group.command("search")
    async def keyword_search(self, event: AstrMessageEvent, keyword: str):
        """搜索关键词"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())

        keywords = await self.keyword_manager.list_keywords(group_id, user_id, keyword)

        if keywords:
            result = f"🔍 搜索结果（包含 '{keyword}'）：\n" + "\n".join(keywords[:10])
            if len(keywords) > 10:
                result += f"\n...共找到 {len(keywords)} 个相关词条"
        else:
            result = f"未找到包含 '{keyword}' 的词条"

        yield event.plain_result(result)

    @keyword_group.command("builtin")
    async def keyword_builtin(self, event: AstrMessageEvent):
        """内置词库管理"""
        yield event.plain_result(
            "📦 内置词库管理\n\n"
            "可用命令：\n"
            "1. /keyword builtin list - 查看所有内置关键词\n"
            "2. /keyword builtin import [词库ID] - 导入内置词库到指定词库（不指定则导入到当前词库）\n"
        )

    @keyword_group.command("builtin list")
    async def keyword_builtin_list(self, event: AstrMessageEvent):
        """列出内置关键词"""
        builtin_items = []
        for i, item in enumerate(self.keyword_manager.builtin_keywords, 1):
            builtin_items.append(
                f"{i}. 【{item['keyword']}】\n"
                f"   模式: {'模糊' if item['mode'] == 0 else '精确'}\n"
                f"   描述: {item['description']}\n"
                f"   回复数: {len(item['responses'])}\n"
            )
        
        result = "🌟 内置关键词列表 🌟\n\n" + "\n".join(builtin_items)
        yield event.plain_result(result)

    @keyword_group.command("builtin import")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def keyword_builtin_import(self, event: AstrMessageEvent, target_lexicon: str = ""):
        """导入内置词库到指定词库"""
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id())
        
        # 如果没有指定词库ID，使用当前词库
        if not target_lexicon:
            lexicon_id = self.keyword_manager.get_lexicon_id(group_id, user_id)
        else:
            lexicon_id = target_lexicon
        
        # 加载目标词库
        target_data = await self.keyword_manager.get_lexicon(lexicon_id, "")
        imported_count = 0
        
        # 导入内置关键词
        for item in self.keyword_manager.builtin_keywords:
            keyword = item["keyword"]
            
            # 检查是否已存在
            exists = False
            for existing_item in target_data["work"]:
                if keyword in existing_item:
                    exists = True
                    break
            
            if not exists:
                new_item = {
                    keyword: {
                        "r": item["responses"],
                        "s": item["mode"]
                    }
                }
                target_data["work"].append(new_item)
                imported_count += 1
        
        # 保存词库
        await self.keyword_manager.save_lexicon(lexicon_id, target_data)
        
        yield event.plain_result(
            f"✅ 导入完成！\n"
            f"目标词库: {lexicon_id}\n"
            f"导入数量: {imported_count}/{len(self.keyword_manager.builtin_keywords)}"
        )

    async def terminate(self):
        logger.info("Van词库插件正在卸载...")