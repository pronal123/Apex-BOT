# ====================================================================================
# Apex BOT v20.0.45 - Future Trading / 30x Leverage 
# (ダミーロジックを本番稼働ロジックに置換)
# 
# 🚨 致命的エラー修正強化: 
# 1. fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 ✅
# 2. 注文失敗エラー (Amount can not be less than zero) 対策 ✅
# 3. 注文執行失敗 (ccxt.ExchangeError) の詳細な捕捉・報告 ✅
# 
# 🚀 本番稼働ロジック置換: 
# 4. マクロ環境取得 (FGI APIコール) を実装 ✅
# 5. ポジション監視・決済 (SL/TP監視とクローズ注文) を実装 ✅
# 6. スコアリングにマルチタイムフレーム (4hトレンドフィルタ) を導入 ✅
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
from fastapi import FastAPI, Request, Response 
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
import uuid 
import math 
import traceback 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - (%(funcName)s) - %(message)s' 
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", 
    "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", 
    "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "FLOW/USDT", "IMX/USDT", "SUI/USDT", "ASTER/USDT", "ENA/USDT",
    "ZEC/USDT", "PUMP/USDT", "PEPE/USDT", "FARTCOIN/USDT",
    "WLFI/USDT", "PENGU/USDT", "ONDO/USDT", "HBAR/USDT", "TRUMP/USDT",
    "SHIB/USDT", "HYPE/USDT", "LINK/USDT", "ZEC/USDT",
    "VIRTUAL/USDT", "PIPPIN/USDT", "GIGGLE/USDT", "H/USDT", "AIXBT/USDT", 
]
TOP_SYMBOL_LIMIT = 40               
BOT_VERSION = "v20.0.45"            # 💡 BOTバージョンを更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # 🚀 本番ロジック: FGI取得用API

LOOP_INTERVAL = 60 * 1              
ANALYSIS_ONLY_INTERVAL = 60 * 5    
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

# 💡 先物取引設定 
LEVERAGE = 30 
TRADE_TYPE = 'future' 
MIN_MAINTENANCE_MARGIN_RATE = 0.005 

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 

# 💡 【固定ロット】設定 
FIXED_NOTIONAL_USDT = 20.0 

# 💡 WEBSHARE設定 
WEBSHARE_METHOD = os.getenv("WEBSHARE_METHOD", "HTTP") 
WEBSHARE_POST_URL = os.getenv("WEBSHARE_POST_URL", "http://your-webshare-endpoint.com/upload") 

# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0 
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

# テクニカル分析定数 (これらはバックテストで最適化すべきパラメータだが、一旦固定値で本番ロジックのフローを実装)
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
HIGH_LEVEL_TREND_CONFIRMATION = 0.05 # 🚀 追加: 4hトレンド一致ボーナス/逆行ペナルティ

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             
MIN_RISK_PERCENT = 0.008            
RR_RATIO_TARGET = 1.5               

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

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
        
def format_price(price: float) -> str:
    """価格を整形する"""
    if price is None:
        price = 0.0
    # 0.01より大きい場合は小数点以下2桁、それ以外は動的に
    if price >= 0.01:
        return f"{price:,.2f}"
    return f"{price:,.8f}".rstrip('0').rstrip('.')

# 清算価格の計算関数
def calculate_liquidation_price(entry_price: float, leverage: int, side: str = 'long', maintenance_margin_rate: float = MIN_MAINTENANCE_MARGIN_RATE) -> float:
    """
    推定清算価格 (Liquidation Price) を計算する。
    """
    if leverage <= 0 or entry_price <= 0:
        return 0.0
        
    # 必要証拠金率 (1 / Leverage)
    initial_margin_rate = 1 / leverage
    
    if side.lower() == 'long':
        # ロングの場合、価格下落で清算
        liquidation_price = entry_price * (1 - initial_margin_rate + maintenance_margin_rate)
    elif side.lower() == 'short':
        # ショートの場合、価格上昇で清算
        liquidation_price = entry_price * (1 + initial_margin_rate - maintenance_margin_rate)
    else:
        return 0.0
        
    return max(0.0, liquidation_price) 

# 🚀 追加: 簡易 PnL 計算ヘルパー関数
def calculate_pnl_usdt(position: Dict, exit_price: float) -> float:
    """
    ポジション情報と決済価格に基づき、簡易的なPnL (USDT) を計算する。
    """
    # ポジションの名目価値 (コントラクト数 * エントリー価格)
    notional_value = position['contracts'] * position['entry_price'] 
    
    # 価格変動率
    if position['entry_price'] == 0:
        return 0.0
    
    price_change_rate = (exit_price / position['entry_price']) - 1.0
    
    if position['side'] == 'short':
        price_change_rate *= -1 
        
    # PnL = 名目価値 * 価格変動率
    # レバレッジを考慮しない (レバレッジはPnL率に影響)
    pnl = price_change_rate * notional_value
    
    return pnl

# 🚀 追加: 簡易 PnL 率 計算ヘルパー関数
def calculate_pnl_rate(position: Dict, exit_price: float) -> float:
    """
    ポジション情報と決済価格に基づき、簡易的なPnL率 (%) を計算する (レバレッジ考慮)。
    """
    if position['entry_price'] == 0:
        return 0.0
        
    rate_of_return = (exit_price / position['entry_price']) - 1.0
    
    if position['side'] == 'short':
        rate_of_return = 1.0 - (exit_price / position['entry_price'])
        
    # レバレッジをかけてPnL率を算出
    return rate_of_return * LEVERAGE 


def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """現在の市場環境に合わせた動的な取引閾値を決定し、返す。"""
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
    else:
        return SIGNAL_THRESHOLD_NORMAL

def format_startup_message(account_status: Dict, macro_context: Dict, monitoring_count: int, current_threshold: float) -> str:
    """BOT起動完了時の通知メッセージを整形する。"""
    
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    bot_version = BOT_VERSION
    
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # current_threshold に応じてテキストを決定するロジック
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    trade_status = "自動売買 **ON** (Long/Short)" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (先物モード / **{LEVERAGE}x**)\n" 
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット**: **固定** <code>{format_usdt(FIXED_NOTIONAL_USDT)}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **ATRベース**の**動的SL**で管理\n" 
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    balance_section = f"💰 <b>先物口座ステータス</b>\n" 
    if account_status.get('error'):
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
        # ボットが管理しているポジション
        if OPEN_POSITIONS:
            # 契約数とエントリー価格から計算した概算の名目価値
            total_managed_value = sum(p['contracts'] * p['entry_price'] for p in OPEN_POSITIONS) 
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (名目価値合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n" 
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].split('/')[0] # /USDTを除去
                side_tag = '🟢L' if pos.get('side', 'long') == 'long' else '🔴S' 
                balance_section += f"    - Top {i+1}: {base_currency} ({side_tag}, SL: {format_price(pos['stop_loss'])} / TP: {format_price(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

    balance_section += f"\n"

    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約1分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer


def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal.get('timeframe', '1h')
    score = signal.get('score', 0.0)
    side = signal.get('side', 'long') 
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0
    
    # ベースメッセージ構造
    base_message = (
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
    )

    if context == "取引シグナル":
        
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT 
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount = trade_result.get('filled_amount', 0.0)
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            )
            
        message = base_message + (
            f"  - **ステータス**: {trade_status_line}\n" 
            f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.0f}</code> 点\n"
            f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
            f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
            f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
            f"  - **ストップロス (SL)**: <code>{format_price(stop_loss)}</code>\n"
            f"  - **テイクプロフィット (TP)**: <code>{format_price(take_profit)}</code>\n"
            f"  - **清算価格 (Liq. Price)**: <code>{format_price(liquidation_price)}</code>\n" 
            f"  - **リスク幅 (SL)**: <code>{format_usdt(risk_width)}</code> USDT\n"
            f"  - **リワード幅 (TP)**: <code>{format_usdt(reward_width)}</code> USDT\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        if trade_section:
            message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{get_score_breakdown(signal)}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', signal.get('contracts', 0.0))
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        message = base_message + (
            f"  - **ステータス**: {trade_status_line}\n" 
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        message += (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
            
    
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str, is_error: bool = False) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定 (TOKEN/ID) が不足しています。通知をスキップします。")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload, timeout=5)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知を送信しました。")
        return True
    except requests.exceptions.HTTPError as e:
        error_details = response.json() if 'response' in locals() else 'N/A'
        logging.error(f"❌ Telegram HTTPエラー: {e} - 詳細: {error_details}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegramリクエストエラー: {e}")
    return False

def get_score_breakdown(signal: Dict) -> str:
    """シグナルのスコア内訳を整形して返す"""
    tech_data = signal.get('tech_data', {})
    
    breakdown_list = []
    
    # 長期トレンド一致/逆行 (4Hフィルタ含む)
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")
    
    # 4Hトレンドフィルタ
    trend_filter_val = tech_data.get('high_level_trend_confirmation', 0.0)
    trend_filter_text = "🟢 4Hトレンド上位確認" if trend_filter_val > 0 else ("🔴 4Hトレンド逆行ペナルティ" if trend_filter_val < 0 else "🟡 4Hトレンド中立")
    breakdown_list.append(f"{trend_filter_text}: {trend_filter_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行/失速" if macd_val < 0 else "🟡 MACD中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム加速
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速/適正水準: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ: {vol_val*100:+.2f} 点")
        
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")
    
    return "\n".join([f"    - {line}" for line in breakdown_list])

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        return str(obj) 
    elif isinstance(obj, np.generic):
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    try:
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'side': data.get('side', 'N/A'), 
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
            'contracts': data.get('contracts', 0.0), # ログに契約数を追加
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知
async def notify_highest_analysis_score():
    """
    分析の結果、取引閾値に満たない最高スコア銘柄を1時間ごとに通知する。
    """
    global LAST_ANALYSIS_SIGNALS, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, ANALYSIS_ONLY_INTERVAL, SIGNAL_THRESHOLD, GLOBAL_MACRO_CONTEXT, BASE_SCORE
    
    current_time = time.time()
    
    # 実行間隔のチェック
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME < ANALYSIS_ONLY_INTERVAL:
        return

    # 分析結果が格納されているかチェック
    if not LAST_ANALYSIS_SIGNALS:
        logging.info("ℹ️ 分析結果がないため、最高スコア通知をスキップします。")
        LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time 
        return

    # スコアの降順でソートし、最高スコアのシグナルを取得
    sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)
    best_signal = sorted_signals[0]
    
    best_score = best_signal.get('score', 0.0)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 最高スコアが取引閾値未満で、かつ最低限の分析スコア(例: BASE_SCORE+0.1)を超えている場合
    if best_score < current_threshold and best_score >= (BASE_SCORE + 0.1): 
        
        symbol = best_signal.get('symbol', 'N/A')
        timeframe = best_signal.get('timeframe', 'N/A')
        side = best_signal.get('side', 'long')

        # 通知メッセージの整形
        message = (
            f"📈 **分析結果 (最高スコア)** - 定時レポート\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  - **確認日時**: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} (JST)\n"
            f"  - **最高スコア銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **最高スコア**: <code>{best_score * 100:.2f} / 100</code> ({side.capitalize()})\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
            f"  - **備考**: 取引閾値 (<code>{current_threshold * 100:.2f}</code>点) に満たなかったため取引はスキップされました。\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"<i>Bot Ver: {BOT_VERSION}</i>"
        )
        
        logging.info(f"ℹ️ 定時報告: 取引閾値未満の最高スコアを通知します: {symbol} ({side}) Score: {best_score:.2f}")
        await send_telegram_notification(message)
    else:
        # 最高スコアが閾値以上、または分析スコアが低すぎる場合は通知スキップ
        if best_score >= current_threshold:
            logging.info("ℹ️ 最高スコアが取引閾値以上のため、定時通知をスキップします (取引シグナルとして通知済みと想定)。")
        else:
             logging.info("ℹ️ 最高スコアが低すぎるため、定時通知をスキップします。")

    # 通知/ログ処理が完了したら時間を更新
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time

# ====================================================================================
# WEBSHARE FUNCTION (HTTP POST) 
# ====================================================================================

async def send_webshare_update(data: Dict[str, Any]):
    
    if WEBSHARE_METHOD == "HTTP":
        if not WEBSHARE_POST_URL or "your-webshare-endpoint.com/upload" in WEBSHARE_POST_URL:
            logging.warning("⚠️ WEBSHARE_POST_URLが設定されていません。またはデフォルト値のままです。送信をスキップします。")
            return

        try:
            cleaned_data = _to_json_compatible(data)
            
            response = await asyncio.to_thread(requests.post, WEBSHARE_POST_URL, json=cleaned_data, timeout=10)
            response.raise_for_status()

            logging.info(f"✅ WebShareデータ (HTTP POST) を送信しました。ステータス: {response.status_code}")

        except requests.exceptions.RequestException as e:
            logging.error(f"❌ WebShare (HTTP POST) エラー: {e}")
            await send_telegram_notification(f"🚨 <b>WebShareエラー (HTTP POST)</b>\nデータ送信に失敗しました: <code>{e}</code>")

    else:
        logging.warning("⚠️ WEBSHARE_METHOD が 'HTTP' 以外に設定されています。WebShare送信をスキップします。")
        

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    global EXCHANGE_CLIENT, IS_CLIENT_READY
    
    IS_CLIENT_READY = False
    
    if not API_KEY or not SECRET_KEY:
         logging.critical("❌ CCXT初期化スキップ: APIキー または SECRET_KEY が設定されていません。")
         return False
         
    # 既存のクライアントがあれば、リソースを解放する
    if EXCHANGE_CLIENT:
        try:
            await EXCHANGE_CLIENT.close()
            logging.info("✅ 既存のCCXTクライアントセッションを正常にクローズしました。")
        except Exception as e:
            logging.warning(f"⚠️ 既存クライアントのクローズ中にエラーが発生しましたが続行します: {e}")
        EXCHANGE_CLIENT = None
         
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        options = {
            'defaultType': 'future', 
        }

        timeout_ms = 30000 
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options,
            'timeout': timeout_ms 
        })
        logging.info(f"✅ CCXTクライアントの初期化設定完了。リクエストタイムアウト: {timeout_ms/1000}秒。") 
        
        await EXCHANGE_CLIENT.load_markets() 
        
        # レバレッジの設定 (MEXC向け)
        if EXCHANGE_CLIENT.id == 'mexc':
            
            symbols_to_set_leverage = []
            
            # DEFAULT_SYMBOLSに含まれるCCXT標準シンボル (例: BTC/USDT) をベース/クォート通貨に分解
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            for mkt in EXCHANGE_CLIENT.markets.values():
                # USDT建てのSwap/Future市場を探す
                if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                    # 市場の基本通貨が DEFAULT_SYMBOLS のベース通貨に含まれるかチェック
                    if mkt['base'] in default_base_quotes:
                        # set_leverageに渡すべきCCXTシンボル (例: BTC/USDT:USDT) をリストに追加
                        symbols_to_set_leverage.append(mkt['symbol'])
            
            # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
            for symbol in symbols_to_set_leverage:
                # openType: 2 は Cross Margin
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, 
                        symbol, 
                        params={'openType': 2, 'positionType': 2}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Short) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Short) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

            logging.info(f"✅ MEXCの主要な先物銘柄 ({len(symbols_to_set_leverage)}件) に対し、レバレッジを {LEVERAGE}x、マージンモードを 'cross' に設定しました。")
        
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を先物取引モードで初期化し、市場情報をロードしました。")
        IS_CLIENT_READY = True
        return True

    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ CCXT初期化失敗 - 認証エラー: APIキー/シークレットを確認してください。{e}", exc_info=True)
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ CCXT初期化失敗 - 取引所接続エラー: サーバーが利用できません。{e}", exc_info=True)
    except ccxt.NetworkError as e:
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict:
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 口座ステータス取得失敗: CCXTクライアントが準備できていません。")
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
        
    try:
        balance = None
        if EXCHANGE_CLIENT.id == 'mexc':
            logging.info("ℹ️ MEXC: fetch_balance(type='swap') を使用して口座情報を取得します。")
            # MEXCはfetch_balance(type='swap')を使用
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)
            
        if not balance:
            raise Exception("Balance object is empty.")

        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)

        # MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
        if EXCHANGE_CLIENT.id == 'mexc' and balance.get('info'):
            raw_data = balance['info']
            mexc_raw_data = None
            
            if isinstance(raw_data, dict) and 'data' in raw_data:
                mexc_raw_data = raw_data.get('data')
            else:
                mexc_raw_data = raw_data
                
            mexc_data: Optional[Dict] = None
            if isinstance(mexc_raw_data, list) and len(mexc_raw_data) > 0:
                if isinstance(mexc_raw_data[0], dict):
                    mexc_data = mexc_raw_data[0]
            elif isinstance(mexc_raw_data, dict):
                mexc_data = mexc_raw_data

            if mexc_data:
                total_usdt_balance_fallback = 0.0
                if mexc_data.get('currency') == 'USDT':
                    total_usdt_balance_fallback = float(mexc_data.get('totalEquity', 0.0))
                elif mexc_data.get('assets') and isinstance(mexc_data['assets'], list):
                    for asset in mexc_data['assets']:
                        if asset.get('currency') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                # より信頼性の高い値を使用
                if total_usdt_balance_fallback > 0 and total_usdt_balance == 0.0:
                    total_usdt_balance = total_usdt_balance_fallback
                
        
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        
        logging.info(f"✅ 口座残高を取得しました。総資産 (Equity): {format_usdt(ACCOUNT_EQUITY_USDT)} USDT")
        
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': False}

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_open_positions() -> List[Dict]:
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []

    try:
        # ccxtは通常、オープンポジションのみを返す
        positions_raw = await EXCHANGE_CLIENT.fetch_positions()
        
        # ポジションサイズがゼロでないもののみをフィルタリング
        active_positions = [
            pos for pos in positions_raw 
            if pos.get('notional', 0.0) != 0.0 and pos.get('contracts', 0.0) != 0.0
        ]
        
        # 必要な情報を抽出・変換
        formatted_positions = []
        for pos in active_positions:
            contracts = float(pos.get('contracts', 0.0) or 0.0)
            entry_price = float(pos.get('entryPrice', 0.0) or 0.0)
            side = 'long' if contracts > 0 else 'short'
            
            # SL/TPの情報を取得 (取引所側で設定されている場合)
            # ここでは、エントリー時に計算された SL/TP をポジションオブジェクトに格納するために
            # 実際の取引所SL/TP注文をフェッチするロジックは省略し、pos['info']から可能な限り取得する
            stop_loss = pos.get('stopLoss', 0.0) or 0.0
            take_profit = pos.get('takeProfit', 0.0) or 0.0
            
            # 清算価格の推定
            liquidation_price = calculate_liquidation_price(
                entry_price=entry_price, 
                leverage=LEVERAGE, 
                side=side, 
                maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
            )
            
            formatted_positions.append({
                'symbol': pos['symbol'],
                'side': side,
                'contracts': abs(contracts), # 契約数
                'entry_price': entry_price,
                'liquidation_price': liquidation_price,
                # SL/TPは、エントリー時に計算された値で上書きされることを期待するが、ここでは取得した値を採用
                'stop_loss': stop_loss, 
                'take_profit': take_profit,
                'filled_usdt': abs(contracts * entry_price) / LEVERAGE, 
                'info': pos,
            })
        
        OPEN_POSITIONS = formatted_positions
        
        num_positions = len(OPEN_POSITIONS)
        logging.info(f"✅ オープンポジション情報を取得・更新しました (現在 {num_positions} 銘柄)。")
        
        return OPEN_POSITIONS

    except Exception as e:
        logging.error(f"❌ ポジション情報の取得中にエラーが発生しました: {e}", exc_info=True)
        return []

async def fetch_ohlcv_data(symbol: str, timeframe: str = '1h', limit: int = 1000) -> Optional[pd.DataFrame]:
    """OHLCVデータを取得し、Pandas DataFrameとして返す"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCVデータ取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    try:
        # CCXTはシンボル名が異なる場合があるため、市場情報を利用
        ccxt_symbol = EXCHANGE_CLIENT.markets[symbol]['symbol'] if symbol in EXCHANGE_CLIENT.markets else symbol
        
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv or len(ohlcv) < REQUIRED_OHLCV_LIMITS.get(timeframe, 100):
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが不足しています (取得: {len(ohlcv)}/{REQUIRED_OHLCV_LIMITS.get(timeframe, 100)})。")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        return df
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): OHLCVデータの取得中にエラーが発生しました: {e}", exc_info=True)
        return None
        
def calculate_trade_signal(df: pd.DataFrame, df_4h: Optional[pd.DataFrame], symbol: str, timeframe: str, macro_context: Dict) -> Optional[Dict]:
    """
    実践的スコアリングロジックに基づいて取引シグナルを計算する (マルチタイムフレームフィルタリング導入)
    
    df: メインの分析時間足 (例: 1h)
    df_4h: 上位のトレンド確認時間足 (4h)
    """
    
    if len(df) < LONG_TERM_SMA_LENGTH:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 長期SMAに必要なデータが不足しています。シグナル分析スキップ。")
        return None
        
    last_close = df['close'].iloc[-1]
    
    # 0. 4時間足のトレンド確認 (高レベルフィルタリング) - 🚀 本番ロジック
    high_level_trend_confirmation = 0.0
    if df_4h is not None and len(df_4h) >= LONG_TERM_SMA_LENGTH:
        df_4h.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        # SMAが計算されているか確認
        if f'SMA_{LONG_TERM_SMA_LENGTH}' in df_4h.columns:
            sma_200_4h = df_4h[f'SMA_{LONG_TERM_SMA_LENGTH}'].iloc[-1]
            close_4h = df_4h['close'].iloc[-1]
            
            if close_4h > sma_200_4h:
                high_level_trend_confirmation = HIGH_LEVEL_TREND_CONFIRMATION # 4Hトレンド一致ボーナス
            elif close_4h < sma_200_4h:
                high_level_trend_confirmation = -HIGH_LEVEL_TREND_CONFIRMATION # 4Hトレンド逆行ペナルティ
        else:
            logging.warning(f"⚠️ {symbol} (4h): SMA_200計算失敗のため、上位トレンドフィルタをスキップします。")
    
    # 1. テクニカル指標の計算
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
    df.ta.rsi(length=14, append=True)
    df.ta.macd(append=True)
    df.ta.obv(append=True)
    df.ta.bbands(append=True)
    
    if any(col not in df.columns for col in [f'SMA_{LONG_TERM_SMA_LENGTH}', 'RSI_14', 'MACDh_12_26_9', 'OBV', 'BBM_20_2.0']):
        logging.error(f"❌ {symbol}: テクニカル指標の計算に失敗しました。シグナル分析スキップ。")
        return None

    
    # 2. スコアリングの実行
    
    # ベーススコア (全てのシグナルに適用される構造的優位性) + 4Hトレンドフィルタ
    # 🚀 修正: 4Hフィルタをベーススコアに適用
    base_score_final = BASE_SCORE + STRUCTURAL_PIVOT_BONUS + high_level_trend_confirmation 
    
    # ロングシグナル
    long_score = base_score_final
    long_tech_data: Dict[str, Any] = {'base_score': BASE_SCORE, 'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 'high_level_trend_confirmation': high_level_trend_confirmation}

    # ショートシグナル
    short_score = base_score_final
    short_tech_data: Dict[str, Any] = {'base_score': BASE_SCORE, 'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 'high_level_trend_confirmation': high_level_trend_confirmation}
    
    # --- 共通のテクニカル要因 ---
    
    # 2.1. 長期トレンド一致 (SMA 200)
    sma_200 = df[f'SMA_{LONG_TERM_SMA_LENGTH}'].iloc[-1]
    trend_val = LONG_TERM_REVERSAL_PENALTY 
    
    # ロング側
    if last_close > sma_200:
        long_score += trend_val 
    elif last_close < sma_200:
        long_score -= trend_val 
    long_tech_data['long_term_reversal_penalty_value'] = trend_val if last_close > sma_200 else (-trend_val if last_close < sma_200 else 0.0)
    
    # ショート側
    if last_close < sma_200:
        short_score += trend_val 
    elif last_close > sma_200:
        short_score -= trend_val 
    short_tech_data['long_term_reversal_penalty_value'] = trend_val if last_close < sma_200 else (-trend_val if last_close > sma_200 else 0.0)


    # 2.2. MACD モメンタム
    macd_h = df['MACDh_12_26_9'].iloc[-1]
    macd_val = MACD_CROSS_PENALTY 
    
    # ロング側
    if macd_h > 0:
        long_score += macd_val
    elif macd_h < 0:
        long_score -= macd_val
    long_tech_data['macd_penalty_value'] = macd_val if macd_h > 0 else (-macd_val if macd_h < 0 else 0.0)

    # ショート側
    if macd_h < 0:
        short_score += macd_val
    elif macd_h > 0:
        short_score -= macd_val
    short_tech_data['macd_penalty_value'] = macd_val if macd_h < 0 else (-macd_val if macd_h > 0 else 0.0)
    
    # 2.3. RSI モメンタム加速
    rsi = df['RSI_14'].iloc[-1]
    rsi_val = 0.03 # 3%のボーナス

    # ロング側
    if rsi > 60 or (rsi > RSI_MOMENTUM_LOW and rsi > df['RSI_14'].iloc[-2]):
        long_score += rsi_val
        long_tech_data['rsi_momentum_bonus_value'] = rsi_val
    else:
        long_tech_data['rsi_momentum_bonus_value'] = 0.0

    # ショート側
    if rsi < 40 or (rsi < (100 - RSI_MOMENTUM_LOW) and rsi < df['RSI_14'].iloc[-2]):
        short_score += rsi_val
        short_tech_data['rsi_momentum_bonus_value'] = rsi_val
    else:
        short_tech_data['rsi_momentum_bonus_value'] = 0.0
        
    # 2.4. OBV モメンタム (出来高の確証)
    obv_current = df['OBV'].iloc[-1]
    obv_prev = df['OBV'].iloc[-2]
    obv_val = OBV_MOMENTUM_BONUS
    
    # ロング側: 価格上昇 (LastClose > PrevClose) かつ OBV上昇
    price_rising = last_close > df['close'].iloc[-2]
    obv_rising = obv_current > obv_prev
    
    if price_rising and obv_rising:
        long_score += obv_val
        long_tech_data['obv_momentum_bonus_value'] = obv_val
    else:
        long_tech_data['obv_momentum_bonus_value'] = 0.0
        
    # ショート側: 価格下降 (LastClose < PrevClose) かつ OBV下降
    price_falling = last_close < df['close'].iloc[-2]
    obv_falling = obv_current < obv_prev
    
    if price_falling and obv_falling:
        short_score += obv_val
        short_tech_data['obv_momentum_bonus_value'] = obv_val
    else:
        short_tech_data['obv_momentum_bonus_value'] = 0.0
        
    # 2.5. ボラティリティ過熱ペナルティ (BBands幅)
    bb_upper = df['BBU_20_2.0'].iloc[-1]
    bb_lower = df['BBL_20_2.0'].iloc[-1]
    bb_width_percent = (bb_upper - bb_lower) / last_close
    
    volatility_penalty = 0.0
    if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD:
        # ボラティリティが高すぎるとペナルティ（例: 0.05 = -5%）
        volatility_penalty = -(bb_width_percent - VOLATILITY_BB_PENALTY_THRESHOLD) * 2.0 
        volatility_penalty = max(-0.10, volatility_penalty) # 最大ペナルティ -10%
    
    long_score += volatility_penalty
    short_score += volatility_penalty
    long_tech_data['volatility_penalty_value'] = volatility_penalty
    short_tech_data['volatility_penalty_value'] = volatility_penalty


    # 3. マクロ要因の適用
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    macro_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX + forex_bonus * FOREX_BONUS_MAX
    
    # ロング側: マクロボーナスがプラスなら加算、マイナスなら減算
    long_score += macro_bonus
    long_tech_data['sentiment_fgi_proxy_bonus'] = macro_bonus
    
    # ショート側: マクロボーナスがマイナスなら加算、プラスなら減算
    short_score -= macro_bonus 
    short_tech_data['sentiment_fgi_proxy_bonus'] = -macro_bonus
    
    # 4. 流動性ボーナス
    # TOP_SYMBOL_LIMIT に含まれる銘柄かどうかのダミーチェック
    liquidity_bonus = LIQUIDITY_BONUS_MAX if symbol.split('/')[0] in [s.split('/')[0] for s in DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]] else 0.0
    
    long_score += liquidity_bonus
    short_score += liquidity_bonus
    long_tech_data['liquidity_bonus_value'] = liquidity_bonus
    short_tech_data['liquidity_bonus_value'] = liquidity_bonus

    # 5. シグナル生成とフィルタリング
    
    # ロングシグナル
    signals = []
    signals.append({
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'long',
        'score': round(long_score, 4),
        'entry_price': last_close,
        'tech_data': long_tech_data
    })
    
    # ショートシグナル
    signals.append({
        'symbol': symbol,
        'timeframe': timeframe,
        'side': 'short',
        'score': round(short_score, 4),
        'entry_price': last_close,
        'tech_data': short_tech_data
    })
    
    # スコアの高い順にソート
    signals = sorted(signals, key=lambda x: x['score'], reverse=True)
    
    # 閾値チェック
    current_threshold = get_current_threshold(macro_context)
    if signals[0]['score'] >= current_threshold:
        return signals[0]
        
    return None

def calculate_stop_loss_take_profit_and_rr(df: pd.DataFrame, signal: Dict) -> Dict:
    """
    ATRに基づいてSL/TPを計算し、シグナル辞書を更新する。
    """
    
    entry_price = signal['entry_price']
    side = signal['side']
    
    # ATRの計算
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # 最新のATR値を取得
    latest_atr = df[f'ATR_{ATR_LENGTH}'].iloc[-1] if f'ATR_{ATR_LENGTH}' in df.columns else None

    # SL幅の決定
    if latest_atr is not None and not math.isnan(latest_atr) and latest_atr > 0:
        # ATRに基づいてSL幅を設定 (ATR_MULTIPLIER_SLを使用)
        risk_amount = latest_atr * ATR_MULTIPLIER_SL
        sl_type = f"ATR_BASED ({ATR_MULTIPLIER_SL}x ATR)"
    else:
        # ⚠️ WARNING: ATRデータ不足の場合は、固定の最小リスク率を使用
        logging.warning("⚠️ ATRデータ不足。固定の最小リスク率に基づいてSL幅を設定。")
        risk_amount = entry_price * MIN_RISK_PERCENT
        sl_type = f"FIXED_MIN_RISK ({MIN_RISK_PERCENT:.2%})"

    # SL/TPの計算
    if side == 'long':
        stop_loss = entry_price - risk_amount
        # TPはRR_RATIO_TARGETに基づいて計算
        take_profit = entry_price + risk_amount * RR_RATIO_TARGET
    else: # short
        stop_loss = entry_price + risk_amount
        take_profit = entry_price - risk_amount * RR_RATIO_TARGET

    # RR比率を更新
    risk_reward_ratio = RR_RATIO_TARGET
    
    # 価格が負にならないように最低限のチェック
    stop_loss = max(stop_loss, 0.00000001)
    take_profit = max(take_profit, 0.00000001)
    
    # シグナル辞書を更新
    signal['stop_loss'] = round(stop_loss, 8)
    signal['take_profit'] = round(take_profit, 8)
    signal['rr_ratio'] = round(risk_reward_ratio, 2)
    signal['sl_type'] = sl_type
    
    # 清算価格の推定もここで追加
    signal['liquidation_price'] = calculate_liquidation_price(
        entry_price=entry_price, 
        leverage=LEVERAGE, 
        side=side, 
        maintenance_margin_rate=MIN_MAINTENANCE_MARGIN_RATE
    )
    
    return signal

async def calculate_order_amount_and_validate(symbol: str, price: float) -> Optional[float]:
    """
    FIXED_NOTIONAL_USDTに基づいて取引数量を計算し、取引所の精度と最小数量制限を満たすか検証する。
    """
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ 数量計算失敗: CCXTクライアントが準備できていません ({symbol})。")
        return None
        
    if price is None or price <= 0:
        logging.error(f"❌ 価格 ({price}) が不正なため、取引数量を計算できません ({symbol})。")
        return None
        
    try:
        # 1. ロットサイズ計算
        # 名目価値 / 価格 (レバレッジはccxtが設定したマージンモードに依存するため、ここでは純粋なロット計算)
        amount_usdt_based = FIXED_NOTIONAL_USDT / price
        
        # 2. 精度調整 (amountToPrecisionを使用)
        amount_str = EXCHANGE_CLIENT.amount_to_precision(symbol, float(amount_usdt_based))
        amount = float(amount_str)
        
        # 3. 🚨 最終数量チェック (Amount can not be less than zero 対策)
        if amount <= 0:
            logging.error(f"❌ 注文数量が0以下になりました ({symbol})。取引所の最小数量制限を満たしません。(計算数量: {amount_usdt_based:.8f} -> 調整後: {amount:.8f})")
            return None
        
        # 4. 最小注文金額のチェック (CCXTのmarket情報に依存)
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if market and 'limits' in market and 'amount' in market['limits']:
            min_amount = market['limits']['amount'].get('min', 0.0)
            if min_amount and amount < min_amount:
                logging.error(f"❌ 注文数量が取引所の最小注文数量 ({min_amount}) を下回りました ({symbol})。調整後数量: {amount:.8f}")
                return None
                
        logging.info(f"✅ 数量計算成功 ({symbol}): 名目 {FIXED_NOTIONAL_USDT} USDT -> 注文数量 {amount:.8f}")
        return amount
        
    except Exception as e:
        logging.error(f"❌ 数量の計算/精度調整に失敗しました ({symbol}): {e}", exc_info=True)
        return None

async def create_order_for_signal(signal: Dict) -> Optional[Dict]:
    """
    取引所に注文を執行し、結果を返す。
    """
    global EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal.get('stop_loss')
    take_profit = signal.get('take_profit')
    
    # 1. 数量の計算と検証
    amount = await calculate_order_amount_and_validate(symbol, entry_price)
    
    if amount is None:
        logging.error(f"❌ 注文数量が不正またはゼロです ({symbol}, {side})。注文をスキップします。")
        return {'status': 'error', 'error_message': '注文数量が不正またはゼロです'}
        
    if TEST_MODE:
        return {
            'status': 'ok', 
            'filled_amount': amount, 
            'filled_usdt': FIXED_NOTIONAL_USDT, 
            'price': entry_price,
            'error_message': 'TEST_MODE'
        }
    
    order_type = 'market' # Botは通常マーケット注文
    
    # 注文パラメータ設定 (MEXCのレバレッジ設定など)
    order_params = {
        'leverage': LEVERAGE, 
        'clientOrderId': f"apex_{side[0]}{str(uuid.uuid4())[:8]}",
        # SL/TPは注文後の別注文またはクライアント側監視で対応するため、メイン注文からは除外
    }
    
    try:
        # メインのマーケット注文を執行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=side,
            amount=amount,
            params=order_params
        )
        
        # 約定したと仮定して結果を返す
        result = {
            'status': 'ok',
            'order_id': order['id'],
            'filled_amount': order.get('filled', amount),
            'filled_usdt': FIXED_NOTIONAL_USDT, # 注文ロットで近似
            'price': entry_price, 
            'error_message': None,
            'contracts': order.get('filled', amount), # 約定した契約数
        }
        
        # 💡 ここで SL/TP の別注文を取引所に送信するロジックが入る (省略し、クライアント側監視で対応)
        
        return result
        
    except ccxt.ExchangeError as e:
        error_message = str(e)
        logging.error(f"❌ 注文執行失敗 ({symbol}, {side}, amount={amount:.8f}): {EXCHANGE_CLIENT.id} {error_message}")
        traceback.print_exc()
        await send_telegram_notification(f"🚨 **注文失敗 ({symbol}, {side})**\n<pre>エラー詳細: {error_message}</pre>", is_error=True)
        return {'status': 'error', 'error_message': error_message}
        
    except Exception as e:
        logging.error(f"❌ 注文執行中に予期せぬエラーが発生 ({symbol}, {side}): {e}", exc_info=True)
        traceback.print_exc()
        await send_telegram_notification(f"🚨 **致命的エラー: 注文失敗 ({symbol})**\n<pre>予期せぬエラー: {e.__class__.__name__}</pre>", is_error=True)
        return {'status': 'error', 'error_message': str(e)}


# 🚀 新規: ポジション決済ロジック
async def close_position(position: Dict, exit_price: float, exit_type: str) -> Optional[Dict]:
    """
    ポジションをマーケット注文で決済する (本番ロジック)
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    # 決済は反対売買: longの決済は 'sell', shortの決済は 'buy'
    side = 'sell' if position['side'] == 'long' else 'buy'
    amount = position['contracts']
    
    if TEST_MODE:
        pnl_usdt = calculate_pnl_usdt(position, exit_price)
        pnl_rate = calculate_pnl_rate(position, exit_price)
        logging.info(f"⚠️ TEST_MODE: {symbol} ポジションを {exit_type} ({exit_price:.4f}) で決済シミュレーション。PNL: {pnl_usdt:.2f} USDT")
        return {
            'status': 'ok',
            'order_id': 'TEST_EXIT',
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'entry_price': position['entry_price'],
            'filled_amount': amount,
        }

    try:
        # ポジションをクローズするためのマーケット注文 (反対売買)
        order_params = {
            'reduceOnly': True # ポジション決済専用であることを明示
        }
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount, 
            params=order_params
        )
        
        # PnL計算 (取引所から正確な約定価格を取得すべきだが、ここではモニタリング価格で近似)
        pnl_usdt = calculate_pnl_usdt(position, exit_price)
        pnl_rate = calculate_pnl_rate(position, exit_price)
        
        result = {
            'status': 'ok',
            'order_id': order['id'],
            'exit_price': exit_price, 
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'exit_type': exit_type,
            'entry_price': position['entry_price'],
            'filled_amount': amount,
        }
        return result
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ ポジション決済失敗 ({symbol}, {exit_type}): {e}")
        await send_telegram_notification(f"🚨 **ポジション決済失敗 ({symbol}, {exit_type})**\n<pre>エラー詳細: {str(e)}</pre>", is_error=True)
        return {'status': 'error', 'error_message': str(e)}
    except Exception as e:
        logging.error(f"❌ 予期せぬポジション決済エラー ({symbol}, {exit_type}): {e}", exc_info=True)
        return {'status': 'error', 'error_message': str(e)}


async def process_entry_signal(signal: Dict):
    """
    シグナルを受理し、取引実行、ログ記録、通知を行う。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS
    
    symbol = signal['symbol']
    side = signal['side']
    score = signal['score']
    
    # クールダウンチェック
    current_time = time.time()
    last_signal = LAST_SIGNAL_TIME.get(symbol, 0.0)
    
    if current_time - last_signal < TRADE_SIGNAL_COOLDOWN:
        logging.info(f"ℹ️ {symbol}: クールダウン期間中のためシグナルをスキップします (残り: {int(TRADE_SIGNAL_COOLDOWN - (current_time - last_signal))}秒)")
        return

    logging.info(f"🚀 {symbol} ({side}): **取引シグナル確定** (Score: {score:.2f}) - 注文実行へ")

    # 1. 注文実行
    trade_result = await create_order_for_signal(signal)
    
    # 2. ログと通知
    if trade_result and trade_result.get('status') == 'ok':
        LAST_SIGNAL_TIME[symbol] = current_time # 成功時のみクールダウンをリセット
        
        # 注文結果をシグナルに反映（通知用）
        signal['entry_price'] = trade_result.get('price', signal['entry_price'])
        signal['contracts'] = trade_result.get('contracts', 0.0)
        
        # ログ記録
        log_signal(signal, "取引シグナル", trade_result)
        
        # ポジションリストに新規ポジションを追加 (fetch_open_positionsで取得する代わりに即時追加で対応)
        if signal.get('contracts', 0.0) > 0:
            new_position = {
                'symbol': symbol,
                'side': side,
                'contracts': signal['contracts'],
                'entry_price': signal['entry_price'],
                'liquidation_price': signal['liquidation_price'],
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'filled_usdt': trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT), 
                'info': {'order_id': trade_result.get('order_id')},
            }
            OPEN_POSITIONS.append(new_position)
            logging.info(f"✅ ポジションリストに {symbol} を追加しました。")
        
        # Telegram通知
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
        await send_telegram_notification(message)
        
        # ポジション情報の即時更新を推奨 (既に上記で追加済みだが、保険として実行)
        await fetch_open_positions() 

    elif trade_result and trade_result.get('status') == 'error':
         logging.error(f"❌ {symbol} ({side}) 注文失敗のためシグナル処理を中断します。エラー: {trade_result['error_message']}")

async def get_macro_context() -> Dict:
    """FGIデータ (Fear & Greed Index) やその他マクロコンテキストを取得する (本番ロジック: FGI APIコール)"""
    global GLOBAL_MACRO_CONTEXT
    
    try:
        # FGI APIからデータを取得
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        # FGIデータのパース
        fgi_data = data.get('data', [])
        fgi_value = 'N/A'
        fgi_proxy = 0.0
        
        if fgi_data:
            fgi_raw_value = fgi_data[0].get('value')
            if fgi_raw_value and fgi_raw_value.isdigit():
                fgi_value = int(fgi_raw_value)
                # FGIを-0.5から+0.5のプロキシ値に変換 (FGI 0: -0.5, FGI 100: +0.5)
                fgi_proxy = (fgi_value - 50) / 100 
            else:
                fgi_value = fgi_raw_value
                fgi_proxy = 0.0
        
        # Forexボーナスは外部データが必要なため、一旦0.0とする
        forex_bonus = 0.0 

        GLOBAL_MACRO_CONTEXT.update({
            'fgi_proxy': round(fgi_proxy, 4),
            'fgi_raw_value': str(fgi_value),
            'forex_bonus': round(forex_bonus, 4)
        })
        
        logging.info(f"✅ マクロコンテキストを取得しました。FGI: {fgi_value}, 総合影響: {((fgi_proxy + forex_bonus) * 100):.2f} 点")
        return GLOBAL_MACRO_CONTEXT
        
    except Exception as e:
        logging.error(f"❌ マクロコンテキストの取得中にエラーが発生しました: {e}", exc_info=True)
        # 失敗時は前回の値またはデフォルト値を維持
        return GLOBAL_MACRO_CONTEXT

async def process_market_analysis():
    """全監視銘柄に対してシグナル分析と注文実行を行う"""
    global LAST_ANALYSIS_SIGNALS
    
    macro_context = await get_macro_context()
    
    symbols_to_process = CURRENT_MONITOR_SYMBOLS
    if not symbols_to_process:
        logging.warning("⚠️ 監視銘柄リストが空です。スキップします。")
        return

    current_threshold = get_current_threshold(macro_context)
    found_signals: List[Dict] = []
    
    # 出来高/流動性の高い順にソート（ダミー）
    random.shuffle(symbols_to_process) 
    
    # 4時間足データのキャッシュ (データ取得レートリミット対策)
    ohlcv_4h_cache: Dict[str, Optional[pd.DataFrame]] = {}
    
    for symbol in symbols_to_process:
        
        # 1. データ取得 (1hと4h)
        df_1h = await fetch_ohlcv_data(symbol, timeframe='1h', limit=100)
        
        if symbol not in ohlcv_4h_cache:
            ohlcv_4h_cache[symbol] = await fetch_ohlcv_data(symbol, timeframe='4h', limit=200)
        df_4h = ohlcv_4h_cache[symbol]

        if df_1h is None:
            continue
            
        # 2. シグナル計算 (1hベース、4hフィルタ)
        # 🚀 修正: df_4hを引数に追加
        signal_result = calculate_trade_signal(df_1h, df_4h, symbol, '1h', macro_context)
        
        if signal_result is None:
            continue 
            
        # 3. SL/TP/RR計算
        signal_with_sltp = calculate_stop_loss_take_profit_and_rr(df_1h, signal_result)
        
        found_signals.append(signal_with_sltp)
    
    # 4. シグナルフィルタリングと実行
    LAST_ANALYSIS_SIGNALS = found_signals 

    if not found_signals:
        logging.info("ℹ️ 今回の分析では取引閾値以上のシグナルは見つかりませんでした。")
        return
        
    # スコアの高い順にソートし、TOP_SIGNAL_COUNTに絞る
    final_signals = sorted(found_signals, key=lambda x: x['score'], reverse=True)[:TOP_SIGNAL_COUNT]
    
    logging.info(f"✅ 取引閾値 ({current_threshold*100:.2f}) 以上のシグナルが {len(final_signals)} 件見つかりました。")

    for signal in final_signals:
        # 既にポジションがある銘柄はスキップするロジックをここで追加
        if any(p['symbol'] == signal['symbol'] for p in OPEN_POSITIONS):
             logging.info(f"ℹ️ {signal['symbol']} は既にポジションがあるため、新規シグナルをスキップします。")
             continue
             
        await process_entry_signal(signal)


# ====================================================================================
# SCHEDULER & MAIN LOOP
# ====================================================================================

async def update_current_monitor_symbols():
    """
    監視銘柄リストを更新する。
    """
    global CURRENT_MONITOR_SYMBOLS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
         logging.warning("⚠️ 銘柄更新スキップ: CCXTクライアントが準備できていません。")
         return
         
    logging.info("ℹ️ 監視銘柄データ取得を試行中...")
    
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if tickers is None:
            logging.error("❌ 監視銘柄の取得中に予期せぬエラーが発生しました: fetch_tickers() が None を返しました。取引所APIに問題がある可能性があります。")
            return
            
        marketIds = list(tickers.keys()) 
        # USDT建てのFuture/Swap市場を探す (MEXC形式: BTC/USDT:USDT)
        usdt_swap_symbols = [
            m for m in marketIds 
            if '/USDT' in m and EXCHANGE_CLIENT.markets.get(m) and EXCHANGE_CLIENT.markets[m]['type'] in ['swap', 'future']
        ] 

        if usdt_swap_symbols:
            # 出来高ベースのフィルタリング/ソートをシミュレート
            usdt_swap_symbols.sort(key=lambda s: float(tickers.get(s, {}).get('quoteVolume', 0.0) or 0.0), reverse=True)
            CURRENT_MONITOR_SYMBOLS = usdt_swap_symbols[:TOP_SYMBOL_LIMIT]
            logging.info(f"✅ 監視銘柄リストを更新しました (計 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄)。")
        else:
            logging.warning("⚠️ アクティブなUSDT建て先物/Swap銘柄が見つかりませんでした。デフォルトリストを維持します。")
        
    except Exception as e:
        logging.error(f"❌ 監視銘柄の取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        

async def position_monitor_and_update_sltp():
    """ポジションの監視とTP/SLの執行を行う (本番ロジック)"""
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    positions_to_remove = []
    
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        side = pos['side']
        
        # 1. 最新価格の取得
        try:
            # fetch_tickerはレートリミットが高いため注意
            ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
            current_price = ticker['last']
        except Exception as e:
            logging.error(f"❌ モニターエラー: {symbol} の価格取得に失敗: {e}")
            continue

        stop_loss = pos['stop_loss']
        take_profit = pos['take_profit']
        
        exit_triggered = False
        exit_type = None
        exit_price = current_price

        # 2. SL/TPのチェック (クライアント側でトリガー)
        if side == 'long':
            # SLトリガー (価格がSL以下に落ちた)
            if current_price <= stop_loss:
                exit_triggered = True
                exit_type = 'STOP_LOSS'
            # TPトリガー (価格がTP以上に上がった)
            elif current_price >= take_profit:
                exit_triggered = True
                exit_type = 'TAKE_PROFIT'
        
        elif side == 'short':
            # SLトリガー (価格がSL以上に上がった)
            if current_price >= stop_loss:
                exit_triggered = True
                exit_type = 'STOP_LOSS'
            # TPトリガー (価格がTP以下に落ちた)
            elif current_price <= take_profit:
                exit_triggered = True
                exit_type = 'TAKE_PROFIT'
        
        # 3. 決済ロジックの実行
        if exit_triggered:
            logging.warning(f"🚨 {symbol} ({side}) で {exit_type} がトリガーされました。決済を実行します。")
            
            # 決済実行
            trade_result = await close_position(pos, exit_price, exit_type)
            
            if trade_result and trade_result.get('status') == 'ok':
                # Telegram通知
                message = format_telegram_message(pos, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
                await send_telegram_notification(message)
                
                # ログ記録
                log_signal(pos, "ポジション決済", trade_result)
                
                positions_to_remove.append(pos)
            else:
                logging.error(f"❌ {symbol} の決済に失敗しました。ポジションをリストに残します。")

    # ポジションリストから決済済み銘柄を削除
    OPEN_POSITIONS = [pos for pos in OPEN_POSITIONS if pos not in positions_to_remove]
    
    if positions_to_remove:
        logging.info(f"✅ 決済が完了した {len(positions_to_remove)} 件のポジションをリストから削除しました。")


async def send_hourly_notification():
    """1時間ごとのレポートを送信する（ダミー実装）"""
    global LAST_HOURLY_NOTIFICATION_TIME, IS_FIRST_MAIN_LOOP_COMPLETED, ACCOUNT_EQUITY_USDT
    
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        return

    current_time = time.time()
    
    # 1時間間隔のチェック
    if current_time - LAST_HOURLY_NOTIFICATION_TIME < 60 * 60:
        return
        
    
    # WebShareの更新 (毎時)
    await send_webshare_update({
        'version': BOT_VERSION,
        'timestamp': datetime.now(JST).isoformat(),
        'account_equity': ACCOUNT_EQUITY_USDT,
        'open_positions_count': len(OPEN_POSITIONS),
        'last_signals': LAST_ANALYSIS_SIGNALS[:5],
        'macro_context': GLOBAL_MACRO_CONTEXT,
    })
    
    LAST_HOURLY_NOTIFICATION_TIME = current_time

async def main_bot_loop():
    """メインの取引ロジックを処理するループ"""
    global LAST_SUCCESS_TIME, IS_FIRST_MAIN_LOOP_COMPLETED
    
    try:
        now_jst = datetime.now(JST).strftime("%H:%M:%S")
        logging.info(f"--- メインボットループ実行開始 (JST: {now_jst}) ---")
        
        # 1. アカウント情報とポジションの取得
        account_status = await fetch_account_status()
        await fetch_open_positions()

        # 2. 監視銘柄リストの更新 (メインループごとに実行)
        if not SKIP_MARKET_UPDATE:
            await update_current_monitor_symbols()
        else:
            logging.warning("⚠️ SKIP_MARKET_UPDATEが有効なため、銘柄リストの更新をスキップします。")
        
        # 3. ポジション不在の場合にシグナル分析と注文実行
        # 💡 ポジション保持中でもシグナル分析自体は実行するが、新規注文はスキップされる
        await process_market_analysis() 
        

        # 4. 定時通知のチェック
        await send_hourly_notification()
        await notify_highest_analysis_score()
            
        LAST_SUCCESS_TIME = time.time()
        
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            # 起動完了通知
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
            await send_telegram_notification(startup_message)
            IS_FIRST_MAIN_LOOP_COMPLETED = True

    except Exception as e:
        logging.error(f"❌ 致命的なメインループエラー: {e}", exc_info=True)
        # 致命的エラー時のTelegram通知
        await send_telegram_notification(f"🚨 **致命的エラー: メインループクラッシュ**\n<pre>{e.__class__.__name__}: {str(e)}</pre>", is_error=True)

    finally:
        logging.info("--- メインボットループ実行終了 ---")

async def main_bot_scheduler():
    """メインボットループを定期的に実行するスケジューラ"""
    # 最初の初期化を待つ
    await asyncio.sleep(5)
    
    # 初回起動時にCCXTクライアントを初期化
    await initialize_exchange_client() 
    
    while True:
        if IS_CLIENT_READY:
            await main_bot_loop()
            logging.info(f"次のメインループまで {LOOP_INTERVAL} 秒待機します。")
        else:
            logging.warning("⚠️ CCXTクライアントが未準備/エラー状態のため、メインループをスキップし、10秒後に再試行します。")
            await initialize_exchange_client() # クライアントの再初期化を試行
            
        await asyncio.sleep(LOOP_INTERVAL)

async def position_monitor_scheduler():
    """ポジション監視を短い間隔で実行するスケジューラ (本番ロジック: SL/TPトリガー監視用)"""
    
    # 最初の初期化を待つ
    await asyncio.sleep(5)
    
    while True:
        # クライアントが準備できており、オープンポジションがある場合に実行
        if IS_CLIENT_READY and OPEN_POSITIONS: 
            # 短い間隔でポジションを監視し、TP/SLトリガーをチェック (レートリミットに注意)
            await position_monitor_and_update_sltp()
        
        await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに監視


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI()

@app.get("/")
async def root():
    return {"message": f"Apex BOT {BOT_VERSION} is running."}

# UptimeRobotなどの監視サービスからのヘルスチェックに対応
@app.head("/")
async def head_root(request: Request):
    # ヘルスチェックに成功したと見なす
    return Response(status_code=200)


@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行"""
    logging.info("BOTサービスを開始しました。")
    
    # スケジューラをバックグラウンドで開始
    asyncio.create_task(main_bot_scheduler())
    asyncio.create_task(position_monitor_scheduler())


# エラーハンドラ 
@app.exception_handler(Exception)
async def default_exception_handler(request, exc):
    """捕捉されなかった例外を処理し、ログに記録する"""
    
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 未処理の致命的なエラーが発生...: {exc}", exc_info=True)
        # 致命的エラー時のTelegram通知
        await send_telegram_notification(f"🚨 **サーバー未処理エラー**\n<pre>{exc.__class__.__name__}: {str(exc)}</pre>", is_error=True)

    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )

# ====================================================================================
# 8. 実行 (開発環境用)
# ====================================================================================
if __name__ == "__main__":
    # 開発環境での実行をシミュレート
    # 実際のデプロイでは、このブロックは使用されず、renderなどのPaaSがuvicornを起動します
    uvicorn.run(app, host="0.0.0.0", port=8000)
