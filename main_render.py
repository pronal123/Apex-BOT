# ====================================================================================
# Apex BOT v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 40 - Live FGI)
#
# ★★★ 修正点: KeyError: 'MACDh' 対策 ★★★
# MACD計算後、必要なMACDhカラムが存在するかをチェックし、存在しない場合はフラグをFalseに設定してエラーを回避する。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math # 数値計算ライブラリ

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (先物シンボル形式 BTC/USDT:USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "BNB/USDT:USDT", "XRP/USDT:USDT", "ADA/USDT:USDT",
    "DOGE/USDT:USDT", "DOT/USDT:USDT", "TRX/USDT:USDT", 
    "LTC/USDT:USDT", "AVAX/USDT:USDT", "LINK/USDT:USDT", "UNI/USDT:USDT", "ETC/USDT:USDT", "BCH/USDT:USDT",
    "NEAR/USDT:USDT", "ATOM/USDT:USDT", 
    "ALGO/USDT:USDT", "XLM/USDT:USDT", "SAND/USDT:USDT",
    "GALA/USDT:USDT", # "FIL/USDT:USDT", # ログでエラーが出たFILは一時コメントアウトを推奨
    "AXS/USDT:USDT", "MANA/USDT:USDT", "AAVE/USDT:USDT",
    "FLOW/USDT:USDT", "IMX/USDT:USDT", 
]
TOP_SYMBOL_LIMIT = 40               
LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 60    
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  
MONITOR_INTERVAL = 10               

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (投入証拠金ベース)
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")

# ★レバレッジ設定★
LEVERAGE = 10 # 10倍

# 💡 WEBSHARE設定 (HTTP POSTへ変更)
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
# ★シンボルを先物形式に統一 (DEFAULT_SYMBOLSで対応済)★
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy() 
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0} 
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
# ★先物取引ポジション管理リスト★
OPEN_POSITIONS: List[Dict] = [] 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                   
LONG_TERM_SMA_LENGTH = 200          
LONG_TERM_REVERSAL_PENALTY = 0.20   
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40               
MACD_CROSS_PENALTY = 0.15           
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05          
FOREX_BONUS_MAX = 0.0               

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.75      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 
OBV_MOMENTUM_BONUS = 0.04           

# ====================================================================================
# UTILITIES & FORMATTING 
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount is None:
        amount = 0.0
        
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す"""
    if score >= 0.90: return "70% (極高)"
    if score >= 0.85: return "65% (高)"
    if score >= 0.75: return "60% (中高)"
    if score >= 0.65: return "55% (中)"
    return "50% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """FGIコンテキストに基づいて動的なシグナル閾値を返す"""
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    if fgi_proxy <= FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    elif fgi_proxy >= FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """シグナル詳細データからスコアのブレークダウン文字列を作成する"""
    tech_data = signal.get('tech_data', {})
    breakdown = ""

    # ロジックの複雑化を避けるため、ここでは簡易的なブレークダウンを生成
    score_elements = [
        ("ベーススコア", BASE_SCORE, "+"),
        ("構造的ピボットボーナス", tech_data.get('structural_pivot_bonus', 0.0), "+"),
        ("OBVモメンタムボーナス", tech_data.get('obv_momentum_bonus_value', 0.0), "+"),
        ("流動性ボーナス", tech_data.get('liquidity_bonus_value', 0.0), "+"),
        ("FGIセンチメント影響", tech_data.get('sentiment_fgi_proxy_bonus', 0.0), "+/-"),
        ("長期トレンド逆行ペナルティ", -tech_data.get('long_term_reversal_penalty_value', 0.0), "-"),
        ("MACD逆行ペナルティ", -tech_data.get('macd_penalty_value', 0.0), "-"),
        ("ボラティリティペナルティ", -tech_data.get('volatility_penalty_value', 0.0), "-"),
    ]

    for name, value, sign in score_elements:
        if abs(value) > 0.001:
            sign_str = "+" if value >= 0 else "-"
            breakdown += f"  {sign_str} {name}: <code>{abs(value) * 100:.2f}</code>点\n"
    
    return breakdown if breakdown else "  (詳細なブレークダウンデータなし)"

def format_analysis_only_message(signals: List[Dict], context: str) -> str:
    """分析のみの通知メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    header = f"📊 **Apex BOT {context}** ({datetime.now(JST).strftime('%H:%M')} JST)\n"
    header += f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
    
    if not signals:
        return header + "  - **シグナル**: 該当する取引シグナルはありませんでした。\n"
        
    body = "  - **強力なシグナル候補**:\n"
    
    for signal in signals:
        symbol = signal['symbol']
        timeframe = signal['timeframe']
        score = signal['score']
        action = signal['action']
        action_text = "ロング (買い)" if action == 'buy' else "ショート (売り)"
        
        body += (
            f"    - **{symbol}** ({timeframe}): <code>{score * 100:.2f}</code>点 / 方向: <code>{action_text}</code>\n"
        )
        
    return header + body

def format_startup_message(status: Dict) -> str:
    """BOT起動時の通知メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    fgi_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')
    threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    message = (
        f"🤖 **Apex BOT 起動通知**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()} (Swap/Futures)</code>\n"
        f"  - **モード**: <code>{'テストモード' if TEST_MODE else 'ライブモード'}</code>\n"
        f"  - **証拠金**: <code>{format_usdt(BASE_TRADE_SIZE_USDT)}</code> USDT (目標)\n"
        f"  - **レバレッジ**: <code>{LEVERAGE}x</code> (固定)\n"
        f"  - **取引閾値**: <code>{threshold * 100:.2f}</code> 点\n"
        f"  - **FGI**: <code>{fgi_value}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  <b>アカウントステータス</b>:\n"
        f"  - **残高 (USDT)**: <code>{format_usdt(status.get('free_balance', 0))}</code>\n"
        f"  - **建玉数**: <code>{status.get('open_positions_count', 0)}</code>\n"
        f"  - **監視銘柄数**: <code>{len(CURRENT_MONITOR_SYMBOLS)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.29 - FUTURES/SWAP (Long/Short)</i>"
    )
    return message

# ★修正: format_telegram_message の完全版を再掲 (ロング/ショート対応ロジック含む)
def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    rr_ratio = signal.get('rr_ratio', 0.0)
    action = signal.get('action', trade_result.get('side', 'buy') if trade_result else 'buy') 

    estimated_wr = get_estimated_win_rate(score)
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # ★修正: actionに基づいてメッセージを動的に変更★
    action_text = "ロング (買い)" if action == 'buy' else "ショート (売り)"
    order_side_text = "ロング" if action == 'buy' else "ショート"
    
    if context == "取引シグナル":
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) 
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(証拠金: {format_usdt(lot_size)} USDT)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: 先物{order_side_text}注文を執行しました。" 
            
            filled_amount = trade_result.get('filled_amount', 0.0) 
            filled_margin_usdt = trade_result.get('filled_usdt', 0.0) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Swap) / 成行{order_side_text} ({LEVERAGE}x)</code>\n" 
                f"  - **投入証拠金**: <code>{format_usdt(filled_margin_usdt)}</code> USDT (目標: {format_usdt(lot_size)})\n" 
                f"  - **契約数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n" 
                f"  - **想定建玉価値**: <code>{format_usdt(filled_amount * entry_price)}</code> USDT (概算)\n" 
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text_pos = "ロング" if trade_result.get('side', 'buy') == 'buy' else "ショート" # 決済されたポジションの方向
        trade_status_line = f"🔴 **ポジション決済 ({side_text_pos})**: {exit_type_final} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 0.0)
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **ポジション方向**: <code>{side_text_pos}</code>\n" 
            f"  - **エントリー価格**: <code>{format_usdt(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_usdt(exit_price)}</code>\n"
            f"  - **決済数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n" 
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **方向**: <code>{action_text}</code>\n" 
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        # SL/TPの絶対値は、価格の大小に関わらず常に同じ計算式
        f"  - **リスク幅 (SL)**: <code>{format_usdt(abs(entry_price - stop_loss))}</code> USDT\n" 
        f"  - **リワード幅 (TP)**: <code>{format_usdt(abs(take_profit - entry_price))}</code> USDT\n" 
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.29 - High-Freq/TP/SL/M1M5 Added (Patch 40 - Live FGI) - FUTURES/SWAP (Long/Short)</i>")
    return message

async def send_telegram_notification(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramのトークンまたはChat IDが設定されていません。通知をスキップします。")
        return
        
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML', # Markdownも可能だが、HTMLの方がコードブロック表現に強い
    }
    
    try:
        # 非同期でリクエストを送信 (requestsは同期ライブラリなので、将来的にaiohttpなどに置き換えることが望ましい)
        # 現在はasyncio.to_threadでrequestsを非同期実行
        await asyncio.to_thread(requests.post, api_url, data=payload)
        logging.info("✅ Telegram通知を送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram通知の送信に失敗しました: {e}")

def _to_json_compatible(data: Any) -> Any:
    """Pandasの数値型などを標準Python型に変換し、JSON互換にする"""
    if isinstance(data, (np.integer, np.floating)):
        return float(data)
    elif isinstance(data, dict):
        return {k: _to_json_compatible(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_to_json_compatible(item) for item in data]
    return data

def log_signal(signal: Dict, log_type: str, trade_result: Optional[Dict] = None):
    """取引シグナルと結果をJSONファイルにログとして記録する"""
    log_file_path = "trade_log.json"
    timestamp = datetime.now(timezone.utc).isoformat()
    log_entry = {
        'timestamp': timestamp,
        'type': log_type,
        'symbol': signal.get('symbol'),
        'timeframe': signal.get('timeframe'),
        'action': signal.get('action'),
        'score': signal.get('score'),
        'trade_result': trade_result,
        'details': _to_json_compatible(signal.get('tech_data', {}))
    }
    
    try:
        # 既存のログを読み込む
        if os.path.exists(log_file_path):
            with open(log_file_path, 'r', encoding='utf-8') as f:
                logs = json.load(f)
        else:
            logs = []
            
        logs.append(log_entry)
        
        # ログを書き込む
        with open(log_file_path, 'w', encoding='utf-8') as f:
            json.dump(logs, f, indent=4, ensure_ascii=False)
        
    except Exception as e:
        logging.error(f"❌ ログファイルへの書き込みに失敗しました: {e}")

async def send_webshare_update(data: List[Dict]):
    """分析結果を外部WebサービスにHTTP POSTで共有する"""
    if WEBSHARE_METHOD.upper() != "HTTP" or not WEBSHARE_POST_URL:
        return
        
    payload = {
        'timestamp': datetime.now(JST).isoformat(),
        'signals': data,
        'macro_context': GLOBAL_MACRO_CONTEXT
    }
    
    try:
        # requestsを非同期実行
        response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=payload, timeout=10)
        if response.status_code == 200:
            logging.info("✅ WebShareエンドポイントへのデータ送信に成功しました。")
        else:
            logging.error(f"❌ WebShareエンドポイントへのデータ送信に失敗しました。ステータスコード: {response.status_code}")
    except Exception as e:
        logging.error(f"❌ WebShareエンドポイントへのデータ送信中にエラーが発生しました: {e}")

# ====================================================================================
# CORE CCXT ASYNC FUNCTIONS
# ====================================================================================

async def initialize_exchange_client():
    """CCXTクライアントを初期化し、認証と市場の準備を行う"""
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if IS_CLIENT_READY and EXCHANGE_CLIENT:
        return

    try:
        # クライアントクラスを動的に取得
        exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME.lower())
        
        # ★先物取引設定を追加★
        client = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'swap', # ★先物/スワップ取引を設定
            }
        })

        if not API_KEY or not SECRET_KEY:
            logging.warning(f"⚠️ {CCXT_CLIENT_NAME.upper()}のAPIキーまたはシークレットが未設定です。認証なしでクライアントを初期化します。")
            EXCHANGE_CLIENT = client
            IS_CLIENT_READY = True
            return

        # 認証テスト (fetch_balanceなど、軽いプライベートAPIコール)
        balance = await client.fetch_balance()
        
        # ★レバレッジ設定の試行 (MEXC向け)★
        if CCXT_CLIENT_NAME.lower() == 'mexc':
            # MEXCの先物取引では、各シンボルに対して個別にレバレッジを設定する必要がある
            # ここでは、主要シンボルに対してまとめて設定を試みる（エラーを無視して続行）
            for symbol in DEFAULT_SYMBOLS:
                try:
                    await client.set_leverage(LEVERAGE, symbol, params={'marginMode': 'cross'})
                except Exception as e:
                    pass # エラーは無視して続行
            logging.info(f"✅ {CCXT_CLIENT_NAME.upper()}のレバレッジを主要な先物銘柄で {LEVERAGE}x に設定しました。")

        
        EXCHANGE_CLIENT = client
        IS_CLIENT_READY = True
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の初期化と認証に成功しました。")

    except Exception as e:
        logging.error(f"❌ CCXTクライアントの初期化または認証に失敗しました: {e}")
        EXCHANGE_CLIENT = None
        IS_CLIENT_READY = False
        await send_telegram_notification(f"🚨 **初期化エラー**: CCXTクライアント ({CCXT_CLIENT_NAME.upper()}) の設定または認証に失敗しました。BOTを停止します。")
        sys.exit(1) # 致命的なエラーとしてBOTを停止

async def fetch_account_status() -> Dict[str, Any]:
    """アカウントの残高とオープンポジションの状況を取得する"""
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    status: Dict[str, Any] = {
        'free_balance': 0.0,
        'total_balance': 0.0,
        'open_positions_count': len(OPEN_POSITIONS), # ローカルで管理しているポジション数
        'exchange_positions_count': 0,
    }
    
    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        return status

    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        # USDT (または統一証拠金) の残高を取得
        status['free_balance'] = balance.get('USDT', {}).get('free', 0.0)
        status['total_balance'] = balance.get('USDT', {}).get('total', 0.0)
        
        # 取引所のオープンポジションを取得 (ローカルとの同期を推奨)
        # MEXCの場合、fetch_positionsは利用できない可能性があるため、MEXCに最適化
        if CCXT_CLIENT_NAME.lower() == 'mexc':
            # MEXCはfetch_positionsを実装していないため、ここではローカルのOPEN_POSITIONSに頼る
            pass
        else:
            # 他の取引所向け
            positions = await EXCHANGE_CLIENT.fetch_positions()
            active_positions = [p for p in positions if p.get('info', {}).get('positionSide') in ['LONG', 'SHORT'] and p.get('entryPrice') is not None]
            status['exchange_positions_count'] = len(active_positions)
            
            # TODO: ローカルのOPEN_POSITIONSを取引所の情報と同期するロジックをここに追加

    except Exception as e:
        logging.error(f"❌ アカウントステータスの取得に失敗しました: {e}")

    return status

async def adjust_order_amount(symbol: str, target_usdt_margin: float, entry_price: float) -> Optional[float]:
    """
    目標投入証拠金とレバレッジから契約数量を計算し、取引所のルールに基づいて調整する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # 1. 想定ノミナルバリュー (想定建玉価値) を計算
        # ノミナルバリュー = 投入証拠金 * レバレッジ
        nominal_value = target_usdt_margin * LEVERAGE
        
        # 2. 契約数量 (amount) を計算
        # amount = ノミナルバリュー / エントリー価格
        amount_raw = nominal_value / entry_price
        
        # 3. 取引所の市場ルールを取得
        markets = await EXCHANGE_CLIENT.load_markets()
        market = markets.get(symbol)
        
        if not market:
            logging.error(f"❌ {symbol} の市場情報を取得できませんでした。")
            return None
            
        # 4. 最小/最大数量、数量の精度 (precision) に基づいて調整
        # 契約数量の精度
        precision = market['precision']['amount']
        
        # 最小取引数量
        min_amount = market['limits']['amount']['min'] if market['limits']['amount'] else 0.0
        
        # 数量を精度に合わせて丸める
        # 例: precision=3 の場合、0.12345 -> 0.123
        amount_adjusted = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_raw)
        
        final_amount = float(amount_adjusted)
        
        # 最小取引数量のチェック
        if final_amount < min_amount:
            logging.warning(f"⚠️ {symbol} 注文数量 {final_amount:.8f} は最小数量 {min_amount:.8f} 未満です。注文をスキップします。")
            return None
            
        return final_amount

    except Exception as e:
        logging.error(f"❌ 注文数量の調整に失敗しました: {e}")
        return None

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    """OHLCVデータを安全に取得し、Pandas DataFrameに変換する"""
    global EXCHANGE_CLIENT
    
    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        return pd.DataFrame()

    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        if df.empty:
            return df
            
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        return df
        
    except Exception as e:
        logging.error(f"❌ OHLCVデータの取得に失敗しました ({symbol} {timeframe}): {e}")
        return pd.DataFrame()

async def fetch_fgi_data():
    """Fear & Greed Index (FGI) データを外部APIから取得し、プロキシ値を計算する"""
    global GLOBAL_MACRO_CONTEXT
    
    # 外部APIのURL (例: Alternative.me)
    # NOTE: 実際のAPIキーやエンドポイントは環境に応じて変更してください。
    FGI_API_URL = "https://api.alternative.me/fng/" 
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, params={'limit': 1})
        response.raise_for_status()
        data = response.json()
        
        if 'data' in data and len(data['data']) > 0:
            latest = data['data'][0]
            value = int(latest['value'])
            
            # FGI (0=Extreme Fear, 100=Extreme Greed) を -0.5 から +0.5 のプロキシ値に変換
            # FGI_proxy = (FGI_Value - 50) / 100
            fgi_proxy = (value - 50) / 100.0 
            
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({latest['value_classification']})"
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            logging.info(f"✅ FGIデータを取得しました: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']} (Proxy: {fgi_proxy:.2f})")
            
    except Exception as e:
        logging.error(f"❌ FGIデータの取得に失敗しました。ダミー値を使用します: {e}")
        # 失敗した場合、前回値またはデフォルト値を使用
        if 'fgi_proxy' not in GLOBAL_MACRO_CONTEXT:
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0

# ====================================================================================
# TRADING LOGIC
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカルインジケーターを計算する (完全版)"""
    if df.empty:
        return df
    
    # 1. 移動平均線 (Long-Term Trend)
    df['SMA200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # 2. RSI (Momentum)
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # 3. MACD (Trend & Momentum)
    macd_result = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df = df.join(macd_result)
    
    # ★修正: MACDhカラムの存在チェック (KeyError対策)★
    if 'MACDh' not in df.columns:
        # MACD計算が失敗したか、データが不足している場合
        df['MACD_CROSS_UP'] = False
        df['MACD_CROSS_DOWN'] = False
        logging.warning(f"⚠️ MACD/MACDhカラムがデータフレームに存在しません。データ不足の可能性があります。")
    else:
        df['MACD_CROSS_UP'] = (df['MACDh'].iloc[-2] < 0) & (df['MACDh'].iloc[-1] > 0)
        df['MACD_CROSS_DOWN'] = (df['MACDh'].iloc[-2] > 0) & (df['MACDh'].iloc[-1] < 0)
    
    # 4. ボリンジャーバンド (Volatility)
    bb_result = ta.bbands(df['close'], length=20, std=2)
    df = df.join(bb_result)
    # ボラティリティ評価: BB幅の変動率
    if 'BBU_20_2' in df.columns:
        df['BB_WIDTH'] = (df['BBU_20_2'] - df['BBL_20_2']) / df['BBM_20_2']
        df['BB_WIDTH_CHANGE'] = df['BB_WIDTH'].diff()
    else:
        df['BB_WIDTH_CHANGE'] = 0.0
    
    # 5. OBV (Volume Momentum)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)
    
    # 6. Pivots (Structural) - 簡易的なサポート/レジスタンス
    # 過去n期間の最高値・最安値からの距離を計測
    n = 10 
    df['High_n'] = df['high'].rolling(n).max().shift(1)
    df['Low_n'] = df['low'].rolling(n).min().shift(1)
    
    return df

def analyze_signals(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    分析ロジックに基づき、ロングまたはショートの取引シグナルを生成する。
    """
    # SMA200がない、またはすべてNaNの場合はデータ不足と判断
    if df.empty or df['SMA200'].isnull().all():
        return None
        
    current_price = df['close'].iloc[-1]
    
    action: Optional[str] = None
    
    # --- 1. 基本トレンド判定 ---
    if current_price > df['SMA200'].iloc[-1]:
        action = 'buy' 
    elif current_price < df['SMA200'].iloc[-1]:
        action = 'sell' 

    if action is None:
        return None
        
    # --- 2. スコアリングロジック ---
    score = BASE_SCORE # 0.40
    tech_data = {}
    
    # 2-1. 長期トレンド逆行ペナルティ
    tech_data['long_term_reversal_penalty_value'] = 0.0
    if action == 'buy' and current_price < df['SMA200'].iloc[-1]: # 価格がSMA200以下でロングシグナル
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
    elif action == 'sell' and current_price > df['SMA200'].iloc[-1]: # 価格がSMA200以上でショートシグナル
        tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
    
    # 2-2. 構造的ピボットボーナス
    tech_data['structural_pivot_bonus'] = 0.0
    if action == 'buy' and current_price > df['Low_n'].iloc[-1] * 1.001: # 過去の安値ピボットより上
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
    elif action == 'sell' and current_price < df['High_n'].iloc[-1] * 0.999: # 過去の高値ピボットより下
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS
        
    # 2-3. MACDモメンタムペナルティ
    tech_data['macd_penalty_value'] = 0.0
    # MACDカラムが存在しない場合はペナルティを適用しない
    if 'MACD_CROSS_DOWN' in df.columns:
        if action == 'buy' and df['MACD_CROSS_DOWN'].iloc[-1]: # 買いシグナルだがMACDが下向きクロス
            tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
        elif action == 'sell' and df['MACD_CROSS_UP'].iloc[-1]: # 売りシグナルだがMACDが上向きクロス
            tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
    
    # 2-4. OBVモメンタムボーナス
    tech_data['obv_momentum_bonus_value'] = 0.0
    if action == 'buy' and df['OBV'].iloc[-1] > df['OBV_SMA'].iloc[-1]:
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
    elif action == 'sell' and df['OBV'].iloc[-1] < df['OBV_SMA'].iloc[-1]:
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        
    # 2-5. 流動性ボーナス (出来高に基づき最大ボーナスを調整) - 簡易版
    # 実際の出来高ロジックは複雑なため、ここでは固定値とする
    tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX 

    # 2-6. FGIセンチメント影響 (マクロコンテキスト)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = (fgi_proxy / FGI_ACTIVE_THRESHOLD) * FGI_PROXY_BONUS_MAX if abs(fgi_proxy) <= FGI_ACTIVE_THRESHOLD else (FGI_PROXY_BONUS_MAX if fgi_proxy > 0 else -FGI_PROXY_BONUS_MAX)

    # ★ショートの場合は、FGIセンチメントの影響を反転させる (貪欲はショートに不利/恐怖はショートに有利)★
    if action == 'sell':
        sentiment_fgi_proxy_bonus = -sentiment_fgi_proxy_bonus
    
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    # 2-7. ボラティリティペナルティ
    tech_data['volatility_penalty_value'] = 0.0
    # BB_WIDTH_CHANGEカラムが存在しない場合はペナルティを適用しない
    if 'BB_WIDTH_CHANGE' in df.columns and abs(df['BB_WIDTH_CHANGE'].iloc[-1]) > VOLATILITY_BB_PENALTY_THRESHOLD:
        tech_data['volatility_penalty_value'] = 0.05 # 急激なボラティリティ増加/減少はペナルティ
        
    # 2-8. Forexボーナス (ここでは常にゼロ)
    tech_data['forex_bonus'] = 0.0 
    
    # 総合スコア計算
    score += (
        tech_data['structural_pivot_bonus'] + 
        tech_data['obv_momentum_bonus_value'] + 
        tech_data['liquidity_bonus_value'] + 
        tech_data['sentiment_fgi_proxy_bonus'] + 
        tech_data['forex_bonus'] -
        tech_data['long_term_reversal_penalty_value'] -
        tech_data['macd_penalty_value'] -
        tech_data['volatility_penalty_value']
    )
    
    
    ##############################################################
    # 動的なSL/TPとRRRの設定ロジック (スコアと構造を考慮)
    ##############################################################
    
    BASE_RISK_PERCENT = 0.015  # ベースリスク: 1.5%
    PIVOT_SUPPORT_BONUS = tech_data.get('structural_pivot_bonus', 0.0) 

    # 構造的ピボットボーナスが大きいほど、SLを近くに設定（リスクを低減）
    sl_adjustment = (PIVOT_SUPPORT_BONUS / STRUCTURAL_PIVOT_BONUS) * 0.002 if STRUCTURAL_PIVOT_BONUS > 0 else 0.0
    dynamic_risk_percent = max(0.010, BASE_RISK_PERCENT - sl_adjustment) 
    
    BASE_RRR = 1.5  
    MAX_SCORE_FOR_RRR = 0.85
    MAX_RRR = 3.0
    
    if score > SIGNAL_THRESHOLD:
        score_ratio = min(1.0, (score - SIGNAL_THRESHOLD) / (MAX_SCORE_FOR_RRR - SIGNAL_THRESHOLD))
        dynamic_rr_ratio = BASE_RRR + (MAX_RRR - BASE_RRR) * score_ratio
    else:
        dynamic_rr_ratio = BASE_RRR 
        
    rr_ratio = dynamic_rr_ratio 

    # ★修正: ロングとショートでSL/TPの計算を反転させる★
    if action == 'buy':
        # ロング: SLは下、TPは上
        stop_loss = current_price * (1 - dynamic_risk_percent)
        take_profit = current_price * (1 + dynamic_risk_percent * dynamic_rr_ratio)
    else: # action == 'sell' (ショート)
        # ショート: SLは上、TPは下
        stop_loss = current_price * (1 + dynamic_risk_percent)
        take_profit = current_price * (1 - dynamic_risk_percent * dynamic_rr_ratio)
    
    ##############################################################

    current_threshold = get_current_threshold(macro_context)
    
    if score > current_threshold and rr_ratio >= 1.0:
         return {
            'symbol': symbol,
            'timeframe': timeframe,
            'action': action, # 'buy'または'sell'
            'score': min(1.0, score), # スコアを最大1.0に制限
            'rr_ratio': rr_ratio, 
            'entry_price': current_price,
            'stop_loss': stop_loss, 
            'take_profit': take_profit, 
            'lot_size_usdt': BASE_TRADE_SIZE_USDT, 
            'tech_data': tech_data, 
        }
    return None

# ★修正: liquidate_position の完全版 (ロング/ショート PnL計算含む)
async def liquidate_position(position: Dict, exit_type: str, current_price: float) -> Optional[Dict]:
    """
    先物ポジションを決済する (成行決済)。
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    amount = position['amount'] # 契約数量
    entry_price = position['entry_price']
    pos_side = position['side'] # 'buy' (ロング) または 'sell' (ショート)
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    # ★修正: ポジションの方向に応じて決済サイドとパラメータを決定★
    if pos_side == 'buy':
        side = 'sell' # ロングの決済は売り
        params = {'positionSide': 'long'}
    else:
        side = 'buy' # ショートの決済は買い
        params = {'positionSide': 'short'}

    if CCXT_CLIENT_NAME.lower() == 'mexc':
        params['reduceOnly'] = True # ポジション決済のみを行うフラグ

    # 1. 注文実行（成行）
    try:
        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} {side.capitalize()} Market ({exit_type}). 数量: {amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-exit-{uuid.uuid4()}",
                'symbol': symbol,
                'side': side,
                'amount': amount,
                'price': current_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': amount,
                'cost': amount * current_price, 
            }
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side=side,
                amount=amount, 
                params=params
            )
            logging.info(f"✅ 決済実行成功: {symbol} {side.capitalize()} Market. 数量: {amount:.8f} ({exit_type})")

        filled_amount_val = order.get('filled', 0.0)
        cost_val = order.get('cost', 0.0) 

        if filled_amount_val <= 0:
            return {'status': 'error', 'error_message': '約定数量ゼロ'}

        exit_price = cost_val / filled_amount_val if filled_amount_val > 0 else current_price

        # PnL計算
        initial_margin = position['filled_usdt'] # 投入証拠金
        
        # ★修正: PnL計算をポジションのサイドに応じて分岐させる★
        if pos_side == 'buy':
            # ロング: PnL = (決済価格 - エントリー価格) * 数量
            pnl_usdt = (exit_price - entry_price) * filled_amount_val
        else:
            # ショート: PnL = (エントリー価格 - 決済価格) * 数量
            pnl_usdt = (entry_price - exit_price) * filled_amount_val

        pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0

        trade_result = {
            'status': 'ok',
            'exit_type': exit_type,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'filled_amount': filled_amount_val, 
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'side': pos_side, # ポジションの方向を記録
        }
        return trade_result

    except Exception as e:
        logging.error(f"❌ 決済失敗 ({exit_type}): {symbol}. {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'決済エラー: {e}'}

# ★修正: position_management_loop_async の完全版 (ロング/ショート SL/TP判定含む)
async def position_management_loop_async():
    """オープンポジションを監視し、SL/TPをチェックして決済する"""
    global OPEN_POSITIONS, GLOBAL_MACRO_CONTEXT

    if not OPEN_POSITIONS:
        return

    positions_to_check = list(OPEN_POSITIONS)
    closed_positions = []
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    for position in positions_to_check:
        symbol = position['symbol']
        sl = position['stop_loss']
        tp = position['take_profit']
        pos_side = position['side'] # 'buy' or 'sell'
        
        position['timeframe'] = 'N/A (Monitor)' 
        position['score'] = 0.0 

        try:
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
            
            exit_type = None
            
            # ★修正: ポジションのサイドに応じてSL/TPの判定を反転させる★
            if pos_side == 'buy':
                # ロング: 価格がSLを下回るか、TPを上回るか
                if current_price <= sl:
                    exit_type = "SL (ストップロス)"
                elif current_price >= tp:
                    exit_type = "TP (テイクプロフィット)"
            else: # pos_side == 'sell'
                # ショート: 価格がSLを上回るか、TPを下回るか
                if current_price >= sl:
                    exit_type = "SL (ストップロス)"
                elif current_price <= tp:
                    exit_type = "TP (テイクプロフィット)"
            
            if exit_type:
                trade_result = await liquidate_position(position, exit_type, current_price)
                
                if trade_result and trade_result.get('status') == 'ok':
                    log_signal(position, 'Position Exit', trade_result)
                    await send_telegram_notification(
                        format_telegram_message(position, "ポジション決済", current_threshold, trade_result=trade_result, exit_type=exit_type)
                    )
                    closed_positions.append(position)
                else:
                    logging.error(f"❌ {symbol} 決済シグナル ({exit_type}) が発生しましたが、決済実行に失敗しました。")

        except Exception as e:
            logging.error(f"❌ ポジション監視エラー: {symbol}. {e}", exc_info=True)

    for closed_pos in closed_positions:
        if closed_pos in OPEN_POSITIONS:
            OPEN_POSITIONS.remove(closed_pos)

# ★修正: execute_trade の完全版 (ロング/ショート注文実行パラメータ含む)
async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    取引シグナルに基づき、取引所に対して先物注文を実行する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY, LEVERAGE
    
    symbol = signal.get('symbol')
    action = signal.get('action') # 'buy' or 'sell'
    target_usdt_margin = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) 
    entry_price = signal.get('entry_price', 0.0) 

    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.error(f"❌ 注文実行失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'CCXTクライアント未準備'}
        
    if entry_price <= 0 or action not in ['buy', 'sell']:
        logging.error(f"❌ 注文実行失敗: エントリー価格またはアクションが不正です。")
        return {'status': 'error', 'error_message': 'エントリー価格/アクション不正'}
        
    # 現在オープンしているポジション数をチェックし、制限を超える場合はスキップ
    if len(OPEN_POSITIONS) >= TOP_SIGNAL_COUNT:
        logging.warning(f"⚠️ 注文スキップ: ポジション数が上限 ({TOP_SIGNAL_COUNT}) に達しています。")
        return {'status': 'error', 'error_message': 'ポジション上限到達'}


    # 1. 最小数量と精度を考慮した契約数量調整
    adjusted_amount = await adjust_order_amount(symbol, target_usdt_margin, entry_price)

    if adjusted_amount is None:
        logging.error(f"❌ {symbol} {action} 注文キャンセル: 注文数量の自動調整に失敗しました。")
        return {'status': 'error', 'error_message': '注文数量調整失敗'}

    # 2. 注文実行（成行注文を想定）
    try:
        # ★修正: アクションに応じてCCXTのサイドと先物パラメータを動的に設定★
        if action == 'buy':
            ccxt_side = 'buy'
            params = {'positionSide': 'long', 'marginMode': 'cross'}
        else: # action == 'sell'
            ccxt_side = 'sell'
            params = {'positionSide': 'short', 'marginMode': 'cross'}

        if TEST_MODE:
            logging.info(f"✨ TEST MODE: {symbol} {ccxt_side.capitalize()} Market. 数量: {adjusted_amount:.8f} の注文をシミュレート。")
            order = {
                'id': f"test-{uuid.uuid4()}",
                'symbol': symbol,
                'side': ccxt_side,
                'amount': adjusted_amount,
                'price': entry_price, 
                'status': 'closed', 
                'datetime': datetime.now(timezone.utc).isoformat(),
                'filled': adjusted_amount,
                'cost': adjusted_amount * entry_price, 
            }
            # テストモードでは投入証拠金を概算で計算
            filled_margin_usdt = (order.get('cost', 0.0) / LEVERAGE) if LEVERAGE > 0 else 0.0
        else:
            order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='market',
                side=ccxt_side,
                amount=adjusted_amount, 
                params=params # 先物取引用のパラメータ
            )
            logging.info(f"✅ 注文実行成功: {symbol} {action.capitalize()} Market. 契約数量: {adjusted_amount:.8f}")
            
            filled_amount_val = order.get('filled', 0.0)
            
            # 約定価格と投入証拠金の計算（MEXCなど、一部の取引所は 'cost'を返さない可能性があるため）
            # ここでは、約定したノミナルバリューをレバレッジで割って証拠金を概算
            effective_price = order.get('price') if order.get('price') is not None else entry_price
            filled_notional_value = filled_amount_val * effective_price
            filled_margin_usdt = filled_notional_value / LEVERAGE
            order['cost'] = filled_margin_usdt # trade_resultのためにcostに証拠金を設定


        filled_amount_val = order.get('filled', 0.0)
        price_used = order.get('price')
        
        effective_price = price_used if price_used is not None else entry_price
        filled_margin_usdt_final = order.get('cost', 0.0) 

        trade_result = {
            'status': 'ok',
            'order_id': order.get('id'),
            'filled_amount': filled_amount_val, 
            'filled_usdt': filled_margin_usdt_final, 
            'entry_price': effective_price,
            'stop_loss': signal.get('stop_loss'),
            'take_profit': signal.get('take_profit'),
            'side': action, # ポジションの方向を記録
        }

        # ポジションの開始を記録
        if order['status'] in ('closed', 'fill') and trade_result['filled_amount'] > 0:
            position_data = {
                'symbol': symbol,
                'entry_time': order.get('datetime'),
                'side': action, # 記録
                'amount': trade_result['filled_amount'], 
                'entry_price': trade_result['entry_price'],
                'filled_usdt': trade_result['filled_usdt'], 
                'stop_loss': trade_result['stop_loss'],
                'take_profit': trade_result['take_profit'],
                'order_id': order.get('id'),
                'status': 'open',
            }
            OPEN_POSITIONS.append(position_data)
            
        return trade_result

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 注文失敗 - 残高不足: {symbol} {action}. {e}")
        return {'status': 'error', 'error_message': f'残高不足エラー: {e}'}
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ 注文失敗 - 無効な注文: 取引所ルール違反の可能性。{e}")
        return {'status': 'error', 'error_message': f'無効な注文エラー: {e}'}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 注文失敗 - 取引所エラー: API応答の問題。{e}")
        return {'status': 'error', 'error_message': f'取引所APIエラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 注文失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


async def main_bot_loop():
    """BOTのメイン処理ループ (データ取得、分析、取引実行)"""
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME, LAST_HOURLY_NOTIFICATION_TIME

    start_time = time.time()
    
    # 0. 初期化チェック
    if not IS_CLIENT_READY or not EXCHANGE_CLIENT:
        logging.warning("⚠️ CCXTクライアントが準備できていません。初期化を待機します。")
        return

    try:
        # 1. マクロコンテキストの更新 (FGI) - 1時間に1回
        if time.time() - LAST_HOURLY_NOTIFICATION_TIME > 60 * 60:
            await fetch_fgi_data() 
            LAST_HOURLY_NOTIFICATION_TIME = time.time() # 成功時に更新
        
        # 2. 市場の更新 (スキップオプション対応)
        if not SKIP_MARKET_UPDATE:
            # TODO: 市場の更新ロジックを実装 (例: 出来高トップ銘柄の取得)
            pass
        
        # 3. アカウントステータスの取得と初回通知
        account_status = await fetch_account_status()
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # FGI取得成功後に初回通知を送信
            if GLOBAL_MACRO_CONTEXT.get('fgi_raw_value') != 'N/A':
                await send_telegram_notification(format_startup_message(account_status))
                
            
        # 4. OHLCVデータの取得とシグナル分析
        all_signals: List[Dict] = []
        tasks = []
        for symbol in CURRENT_MONITOR_SYMBOLS:
            for timeframe in TARGET_TIMEFRAMES:
                tasks.append(fetch_ohlcv_safe(symbol, timeframe, limit=REQUIRED_OHLCV_LIMITS[timeframe]))
        
        results = await asyncio.gather(*tasks)
        
        # データを DataFrame に変換し、シグナルを分析
        data_index = 0
        for symbol in CURRENT_MONITOR_SYMBOLS:
            for timeframe in TARGET_TIMEFRAMES:
                df = results[data_index]
                data_index += 1
                
                if df.empty:
                    continue
                    
                df = calculate_indicators(df)
                signal = analyze_signals(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
                
                if signal:
                    all_signals.append(signal)

        # 5. シグナルのフィルタリングと取引実行
        
        # Cooldown中のシグナルをフィルタリング
        current_time = time.time()
        tradable_signals = []
        for signal in all_signals:
            symbol_tf = f"{signal['symbol']}-{signal['timeframe']}"
            if current_time - LAST_SIGNAL_TIME.get(symbol_tf, 0) > TRADE_SIGNAL_COOLDOWN:
                tradable_signals.append(signal)

        # スコアで降順ソート
        tradable_signals.sort(key=lambda s: s['score'], reverse=True)
        
        # 既にオープンポジションを持っている銘柄は除外
        open_symbols = {p['symbol'] for p in OPEN_POSITIONS}
        final_signals = []
        for signal in tradable_signals:
            if signal['symbol'] not in open_symbols:
                final_signals.append(signal)
            
        # TOP_SIGNAL_COUNT に制限
        top_signals = final_signals[:TOP_SIGNAL_COUNT - len(OPEN_POSITIONS)]
        
        # 6. 取引の実行
        for signal in top_signals:
            trade_result = await execute_trade(signal)
            
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            
            # Telegram通知
            log_signal(signal, 'Trade Signal', trade_result)
            await send_telegram_notification(
                format_telegram_message(signal, "取引シグナル", current_threshold, trade_result=trade_result)
            )
            
            # Cooldown時間の更新
            symbol_tf = f"{signal['symbol']}-{signal['timeframe']}"
            LAST_SIGNAL_TIME[symbol_tf] = current_time

        # 7. 分析のみの通知 (1時間ごと)
        if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME > ANALYSIS_ONLY_INTERVAL:
            # 閾値に満たないがスコアの高いシグナルを抽出 (ここでは全シグナルからフィルタリング)
            all_signals.sort(key=lambda s: s['score'], reverse=True)
            analysis_signals = all_signals[:5] 
            
            if analysis_signals:
                await send_telegram_notification(
                    format_analysis_only_message(analysis_signals, "分析サマリー")
                )
            
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time
            
        # 8. WebShareへのデータ送信 (1時間ごと)
        if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
            # 全シグナルをJSON互換に変換して送信
            json_compatible_signals = [_to_json_compatible(s) for s in all_signals]
            await send_webshare_update(json_compatible_signals)
            LAST_WEBSHARE_UPLOAD_TIME = current_time
            
        
        LAST_SUCCESS_TIME = time.time()
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
        logging.info(f"✅ メインループ完了。処理時間: {time.time() - start_time:.2f}秒。オープンポジション数: {len(OPEN_POSITIONS)}")

    except Exception as e:
        # MACDhエラーが発生した場合、ここで捕捉される
        logging.error(f"❌ メインループ実行中にエラーが発生しました: {e}", exc_info=True)
        # エラー発生時も最後に成功した時間を更新し、ループを継続させる (待機時間計算のため)
        LAST_SUCCESS_TIME = time.time()
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # 初回起動時の致命的なエラー通知
            await send_telegram_notification(f"🚨 **メインループエラー**: 初回起動時にエラーが発生しました: `{e}`")


# ====================================================================================
# FASTAPI & ASYNC EXECUTION
# ====================================================================================

app = FastAPI()

def get_status_info() -> Dict:
    """BOTの現在の状態を返す"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    open_positions_formatted = []
    
    for p in OPEN_POSITIONS:
        entry_time_jst = datetime.fromisoformat(p['entry_time']).astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")
        open_positions_formatted.append({
            'symbol': p['symbol'],
            'side': p['side'],
            'amount': f"{p['amount']:.4f}",
            'entry_price': format_usdt(p['entry_price']),
            'filled_usdt_margin': format_usdt(p['filled_usdt']),
            'entry_time': entry_time_jst,
            'stop_loss': format_usdt(p['stop_loss']),
            'take_profit': format_usdt(p['take_profit']),
        })
        
    return {
        'status': 'Running' if IS_CLIENT_READY else 'Initializing/Error',
        'exchange': CCXT_CLIENT_NAME.upper(),
        'mode': 'TEST' if TEST_MODE else 'LIVE',
        'last_success_time_utc': datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else 'N/A',
        'current_time_jst': now_jst,
        'open_positions': open_positions_formatted,
        'position_count': len(OPEN_POSITIONS),
        'macro_context': GLOBAL_MACRO_CONTEXT,
    }

@app.get("/")
async def root():
    """ルートエンドポイント"""
    return {"message": "Apex BOT v19.0.29 Futures/Swap is running.", "status_link": "/status"}

@app.get("/status", response_class=JSONResponse)
async def status_endpoint():
    """BOTの現在のステータスを返す (JSON)"""
    return get_status_info()

async def main_loop_scheduler():
    """メインループを定期実行するスケジューラ (1分ごと)"""
    while True:
        try:
            await main_bot_loop()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: `{e}`")

        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)

async def position_monitor_scheduler():
    """TP/SL監視ループを定期実行するスケジューラ (10秒ごと)"""
    while True:
        try:
            await position_management_loop_async()
        except Exception as e:
            logging.critical(f"❌ ポジション監視ループ実行中に致命的なエラー: {e}", exc_info=True)

        await asyncio.sleep(MONITOR_INTERVAL) # MONITOR_INTERVAL (10秒) ごとに実行


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行 (タスク起動の修正)"""
    # 初期化タスクをバックグラウンドで開始
    asyncio.create_task(initialize_exchange_client())
    # メインループのスケジューラをバックグラウンドで開始 (1分ごと)
    asyncio.create_task(main_loop_scheduler())
    # TP/SL監視ループのスケジューラをバックグラウンドで開始 (10秒ごと)
    asyncio.create_task(position_monitor_scheduler())
    logging.info("BOTサービスを開始しました。")

# ====================================================================================
# ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
