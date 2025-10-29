# ====================================================================================
# Apex BOT v20.0.46 - Future Trading / 30x Leverage 
# (Feature: v20.0.45機能 + 致命的バグ修正強化)
# 
# 🚨 致命的エラー修正強化 (v20.0.46 NEW): 
# 1. 💡 修正: main_bot_scheduler内の **UnboundLocalError: LAST_SUCCESS_TIME** を修正。
# 2. 💡 修正: get_top_volume_symbols内の **AttributeError: 'NoneType' object has no attribute 'keys'** (fetch_tickers失敗時) を修正。
# 3. 💡 修正: get_account_status内の **ccxt.base.errors.NotSupported: mexc fetchBalance()** エラーを検知し、安全に処理してBOTの続行を可能にしました。
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
BOT_VERSION = "v20.0.46"            # 💡 BOTバージョンを v20.0.46 に更新 
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" 

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
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 # 💡 グローバル変数の初期化を明示
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
TOP_SIGNAL_COUNT = 3                
REQUIRED_OHLCV_LIMITS = {'1m': 500, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 
TARGET_TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 順張りボーナス/逆張りペナルティの値
STRUCTURAL_PIVOT_BONUS = 0.05       
RSI_MOMENTUM_LOW = 40              # RSI 40/60をモメンタム加速の閾値に使用
MACD_CROSS_PENALTY = 0.15          # MACDの方向一致ボーナス/不一致ペナルティ
LIQUIDITY_BONUS_MAX = 0.06          
FGI_PROXY_BONUS_MAX = 0.05         
FOREX_BONUS_MAX = 0.0               

# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0             # SL = 2.0 * ATR
MIN_RISK_PERCENT = 0.008            # 最低リスク幅 (0.8%) 
RR_RATIO_TARGET = 1.5               # 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
OBV_MOMENTUM_BONUS = 0.04           

# 💡 新規追加: スコアの差別化と勝率向上を目的とした新しい分析要素
# StochRSI
STOCHRSI_LENGTH = 14
STOCHRSI_K = 3
STOCHRSI_D = 3
STOCHRSI_BOS_LEVEL = 20            # 買われ過ぎ/売られ過ぎ水準
STOCHRSI_BOS_PENALTY = 0.08        # StochRSIが極端な水準にある場合のペナルティ/ボーナス

# ADX
ADX_LENGTH = 14
ADX_TREND_STRENGTH_THRESHOLD = 25  # ADXが25以上の場合はトレンドが強いと判断
ADX_TREND_BONUS = 0.07             # トレンド確証ボーナス


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
    指定されたエントリー価格、レバレッジ、維持証拠金率に基づき、
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
        # 💥 FIX: MEXCエラー発生時もEquityを表示 (前回値または初期値)
        equity_display = account_status.get('total_usdt_balance', ACCOUNT_EQUITY_USDT)
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
        balance_section += f"  - **推定総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n"
        balance_section += f"  - **備考**: CCXTの制約 ({CCXT_CLIENT_NAME}) により正確な残高情報取得不可。\n"
    else:
        equity_display = account_status['total_usdt_balance'] 
        balance_section += (
            f"  - **総資産 (Equity)**: <code>{format_usdt(equity_display)}</code> USDT\n" 
        )
        
    # ボットが管理しているポジション
    if OPEN_POSITIONS:
        total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS) 
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
    score = signal['score']
    side = signal.get('side', 'long') 
    
    # 価格情報、RR比率は signal 辞書または trade_result から取得 (process_entry_signalで更新されているはず)
    entry_price = signal.get('entry_price', 0.0)
    stop_loss = signal.get('stop_loss', 0.0)
    take_profit = signal.get('take_profit', 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算 (0でないことを前提とする)
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    
    # SL比率 (EntryPriceに対するリスク幅)
    sl_ratio = risk_width / entry_price if entry_price > 0 else 0.0


    if context == "取引シグナル":
        
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) if trade_result else FIXED_NOTIONAL_USDT # 実際に約定した名目価値
        
        trade_type_text = "先物ロング" if side == 'long' else "先物ショート"
        order_type_text = "成行買い" if side == 'long' else "成行売り"
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(notional_value)} USDT, {LEVERAGE}x)" 
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = f"✅ **自動売買 成功**: **{trade_type_text}**注文を執行しました。" 
            
            filled_amount_raw = trade_result.get('filled_amount', 0.0)
            try:
                filled_amount = float(filled_amount_raw)
            except (ValueError, TypeError):
                filled_amount = 0.0
                
            filled_usdt_notional = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) 
            # 💡 修正: リスク額はSL比率とレバレッジで計算
            risk_usdt = abs(filled_usdt_notional * sl_ratio * LEVERAGE) 
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {sl_ratio*100:.2f}%)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            )
            
    elif context == "ポジション決済":
        exit_type_final = trade_result.get('exit_type', exit_type or '不明')
        side_text = "ロング" if side == 'long' else "ショート"
        trade_status_line = f"🔴 **{side_text} ポジション決済**: {exit_type_final} トリガー ({LEVERAGE}x)" 
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        
        filled_amount_raw = trade_result.get('filled_amount', 0.0)
        try:
            filled_amount = float(filled_amount_raw)
            # ポジションをクローズするため、契約数は元のポジションの契約数を反映させるべき
            if 'contracts' in signal:
                 filled_amount = signal['contracts'] 
        except (ValueError, TypeError):
            filled_amount = 0.0
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_price(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_price(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **純損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n" 
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}** ({side.capitalize()})\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
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
        
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{get_score_breakdown(signal)}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: {BOT_VERSION} - Future Trading / {LEVERAGE}x Leverage</i>") 
    return message


async def send_telegram_notification(message: str) -> bool:
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
    
    # トレンド一致/逆行
    trend_val = tech_data.get('long_term_reversal_penalty_value', 0.0)
    trend_text = "🟢 長期トレンド一致" if trend_val > 0 else ("🔴 長期トレンド逆行" if trend_val < 0 else "🟡 長期トレンド中立")
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

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
        # BB幅の割合を計算 (通知用)
        bb_width_raw = tech_data.get('indicators', {}).get('BB_width', 0.0)
        latest_close_raw = signal.get('entry_price', 1.0) # 最新の終値として代用
        bb_ratio_percent = (bb_width_raw / latest_close_raw) * 100
        
        breakdown_list.append(f"🔴 ボラティリティ過熱ペナルティ ({bb_ratio_percent:.2f}%): {vol_val*100:+.2f} 点")
        
    # 🆕 StochRSIペナルティ/ボーナス
    stoch_val = tech_data.get('stoch_rsi_penalty_value', 0.0)
    stoch_k = tech_data.get('stoch_rsi_k_value', np.nan)
    
    if stoch_val < 0:
        breakdown_list.append(f"🔴 StochRSI過熱ペナルティ (K={stoch_k:.1f}): {stoch_val*100:+.2f} 点")
    elif stoch_val > 0:
        breakdown_list.append(f"🟢 StochRSI過熱回復ボーナス (K={stoch_k:.1f}): {stoch_val*100:+.2f} 点")

    # 🆕 ADXトレンド確証
    adx_val = tech_data.get('adx_trend_bonus_value', 0.0)
    adx_raw = tech_data.get('adx_raw_value', np.nan)
    
    if adx_val > 0:
        breakdown_list.append(f"🟢 ADXトレンド確証 (強 - {adx_raw:.1f}): {adx_val*100:+.2f} 点")
        
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
            # 💡 修正: ログに記録する際、Entry/SL/TPも記録
            'entry_price': data.get('entry_price', 0.0),
            'stop_loss': data.get('stop_loss', 0.0),
            'take_profit': data.get('take_profit', 0.0),
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
                        LEVERAGE, symbol, params={'openType': 2, 'positionType': 1}
                    )
                    logging.info(f"✅ {symbol} のレバレッジを {LEVERAGE}x (Cross Margin / Long) に設定しました。")
                except Exception as e:
                    logging.warning(f"⚠️ {symbol} のレバレッジ/マージンモード設定 (Long) に失敗しました: {e}")
                
                # 💥 レートリミット対策として遅延を挿入
                await asyncio.sleep(LEVERAGE_SETTING_DELAY)

                # positionType: 2 は Short (売り) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, symbol, params={'openType': 2, 'positionType': 2}
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
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: {e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXT初期化中に予期せぬエラーが発生しました: {e}", exc_info=True)

    IS_CLIENT_READY = False
    return False

async def get_top_volume_symbols(limit: int) -> List[str]:
    """
    取引所の先物市場から、出来高トップのシンボルを取得し、DEFAULT_SYMBOLSと結合する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not EXCHANGE_CLIENT.markets:
        logging.warning("⚠️ CCXTクライアントが未準備/市場データなし。デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS
    
    try:
        # Ticker情報を全て取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
    except Exception as e:
        # 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
        logging.error(f"❌ fetch_tickersでエラーが発生しました: {e}。デフォルトシンボルを使用します。", exc_info=True)
        return DEFAULT_SYMBOLS
        
    # 💥 FIX: fetch_tickersがNoneまたは空の辞書を返した場合のチェック
    if not tickers:
        logging.error("❌ fetch_tickersが空または無効なデータを返しました。デフォルトシンボルを使用します。")
        return DEFAULT_SYMBOLS

    # 先物/USDT建て、かつアクティブな市場のみをフィルタリング
    future_usdt_tickers = {
        symbol: ticker for symbol, ticker in tickers.items() 
        if ':USDT' in symbol and ticker['info'].get('is_inverse', False) is False and 'future' in ticker['info'].get('productType', '').lower()
        if EXCHANGE_CLIENT.markets.get(symbol) and EXCHANGE_CLIENT.markets[symbol]['active']
    }
    
    # 出来高 (quoteVolume, USDT) に基づいてソート
    # volumeのキーは取引所によって異なるため、'quoteVolume' (USDT/Quote通貨での出来高) を優先
    def get_volume(ticker):
        # quoteVolumeが最も正確だが、無ければbaseVolume * lastPrice
        vol = ticker.get('quoteVolume') or (ticker.get('baseVolume') * ticker.get('last'))
        return vol if vol is not None else 0
        
    sorted_tickers = sorted(
        future_usdt_tickers.values(), 
        key=lambda x: get_volume(x),
        reverse=True
    )

    # 上位N件のシンボルを取得
    top_symbols_ccxt = [ticker['symbol'] for ticker in sorted_tickers[:limit]]
    
    # デフォルトシンボルと統合
    final_symbols = list(set(top_symbols_ccxt + DEFAULT_SYMBOLS))
    
    logging.info(f"✅ 出来高トップ ({limit}件) を取得し、デフォルトシンボルと統合しました。監視銘柄数: {len(final_symbols)}")
    return final_symbols

async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    """指定されたシンボルのOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        logging.error("❌ CCXTクライアントが初期化されていません。")
        return pd.DataFrame()

    try:
        # CCXTのfetch_ohlcvはタイムスタンプ、始値、高値、安値、終値、出来高のリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}): OHLCVデータが空です。")
            return pd.DataFrame()

        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df['symbol'] = symbol
        df['timeframe'] = timeframe
        
        return df
        
    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): DDoS保護トリガー (レート制限) - スキップします。: {e.args[0]}")
    except ccxt.ExchangeError as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): 取引所エラー - スキップします。: {e.args[0]}")
    except ccxt.NetworkError as e:
        logging.warning(f"⚠️ {symbol} ({timeframe}): ネットワークエラー - スキップします。: {e.args[0]}")
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): OHLCVデータ取得中に予期せぬエラー: {e}", exc_info=True)
        
    return pd.DataFrame()


async def get_account_status() -> Dict[str, Any]:
    """
    口座残高、証拠金情報などを取得し、USDT建ての総資産を計算する
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, IS_CLIENT_READY
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ クライアントが未準備のため、口座ステータスの取得をスキップします。")
        return {'error': True, 'message': 'Client not ready', 'total_usdt_balance': ACCOUNT_EQUITY_USDT}
        
    try:
        # 1. fetch_balanceで先物口座（通常は'future'または取引所固有のID）の残高を取得
        try:
            # 💥 FIX: MEXCなどの取引所のために、type='future'を明示的に指定して再試行
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'type': TRADE_TYPE}) 
            
            # ccxtの仕様に基づき、USDT建ての残高情報を抽出
            usdt_balance = balance.get('USDT', {})
            total_usdt_balance = usdt_balance.get('total', 0.0)
            free_usdt_balance = usdt_balance.get('free', 0.0)
            
            # CCXTの総資産 (Equity) の定義を信頼し、USDTの総額を使用
            ACCOUNT_EQUITY_USDT = total_usdt_balance
            
            logging.info(f"💰 口座ステータス更新: 総資産 (Equity) = {format_usdt(ACCOUNT_EQUITY_USDT)} USDT")
            
            return {
                'total_usdt_balance': total_usdt_balance,
                'free_usdt_balance': free_usdt_balance,
                'info': balance.get('info', {})
            }
            
        except ccxt.NotSupported as e:
            # 💥 FIX: MEXCのNotSupportedエラーを捕捉し、BOTは続行
            logging.error(f"❌ 口座ステータス取得エラー (NotSupported): {e.args[0]}。CCXTの制約により、Equityを前回値 ({format_usdt(ACCOUNT_EQUITY_USDT)} USDT) または0として続行します。", exc_info=True)
            return {'error': True, 'message': str(e), 'total_usdt_balance': ACCOUNT_EQUITY_USDT}
            
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        return {'error': True, 'message': str(e), 'total_usdt_balance': ACCOUNT_EQUITY_USDT}


async def fetch_fgi_data() -> float:
    """
    Fear & Greed Index (FGI) を取得し、スコアリング用のプロキシ値を返す
    プロキシ値: [-1.0 (Extreme Fear) 〜 1.0 (Extreme Greed)]
    """
    global GLOBAL_MACRO_CONTEXT
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            fgi_value = int(data['data'][0]['value']) # 0-100
            fgi_classification = data['data'][0]['value_classification']
            
            # 0-100 を -1.0 - 1.0 に変換するプロキシ
            fgi_proxy = (fgi_value / 50.0) - 1.0 
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value} ({fgi_classification})"
            logging.info(f"🌍 FGI更新: {fgi_value} ({fgi_classification}), Proxy: {fgi_proxy:+.3f}")
            
            return fgi_proxy
            
    except Exception as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生しました: {e}")
    
    # 失敗した場合は中立値
    GLOBAL_MACRO_CONTEXT['fgi_proxy'] = 0.0
    GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = 'N/A'
    return 0.0


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC (v20.0.46 完全に再現)
# ====================================================================================

# MACDのデフォルトパラメータ
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
MACD_PARAMS = f'{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}'
MACD_COL = f'MACD_{MACD_PARAMS}'
MACDS_COL = f'MACDS_{MACD_PARAMS}'
MACDH_COL = f'MACDH_{MACD_PARAMS}' # エラーの原因となったキー

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    データフレームに各種テクニカルインジケーターを追加します。
    (MACDH KeyErrorの対策を組み込みました)

    Args:
        df (pd.DataFrame): OHLCVデータを含むデータフレーム。

    Returns:
        pd.DataFrame: インジケーター列が追加されたデータフレーム。
    """
    if df.empty:
        return df

    symbol = df['symbol'].iloc[0] if 'symbol' in df.columns else 'N/A'
    timeframe = df['timeframe'].iloc[0] if 'timeframe' in df.columns else 'N/A'

    try:
        # 1. MACD (Moving Average Convergence Divergence) の計算
        macd_results = df.ta.macd(
            close='close', 
            fast=MACD_FAST, 
            slow=MACD_SLOW, 
            signal=MACD_SIGNAL, 
            append=False
        )

        # MACDH KeyError対策: 結果の列をチェックして代入
        if MACD_COL in macd_results.columns:
            df['MACD'] = macd_results[MACD_COL]
        else:
            df['MACD'] = np.nan
        
        if MACDS_COL in macd_results.columns:
            df['MACD_S'] = macd_results[MACDS_COL]
        else:
            df['MACD_S'] = np.nan
            
        # 💡 MACDH KeyErrorの修正箇所 (計算失敗時の保護)
        if MACDH_COL in macd_results.columns:
            df['MACD_H'] = macd_results[MACDH_COL]
        else:
            logging.warning(f"⚠️ {symbol} ({timeframe}): MACDヒストグラムのキー {MACDH_COL} が見つかりませんでした。データ不足の可能性。")
            df['MACD_H'] = np.nan 

        # 2. RSI (Relative Strength Index)
        df.ta.rsi(length=STOCHRSI_LENGTH, append=True) # StochRSIのRSI期間を使用
        
        # 3. Bollinger Bands (BBANDS)
        bbands_results = df.ta.bbands(append=False)
        if 'BBL_5_2.0' in bbands_results.columns:
            df['BB_low'] = bbands_results['BBL_5_2.0']
            df['BB_mid'] = bbands_results['BBM_5_2.0']
            df['BB_high'] = bbands_results['BBU_5_2.0']
            df['BB_width'] = bbands_results['BBB_5_2.0'] # BB幅 (BB_width)
        else:
             df['BB_low'] = df['BB_mid'] = df['BB_high'] = df['BB_width'] = np.nan

        # 4. OBV (On-Balance Volume)
        df.ta.obv(append=True)
        
        # 5. SMA 200 (長期トレンドの判断)
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True)
        
        # 6. ATR (Average True Range)
        df.ta.atr(length=ATR_LENGTH, append=True)
        
        # 7. ADX (Average Directional Index) (v20.0.44追加機能)
        adx_results = df.ta.adx(length=ADX_LENGTH, append=False)
        ADX_COL = f'ADX_{ADX_LENGTH}'
        if ADX_COL in adx_results.columns:
            df['ADX'] = adx_results[ADX_COL]
            df['DMP'] = adx_results[f'DMP_{ADX_LENGTH}']
            df['DMN'] = adx_results[f'DMN_{ADX_LENGTH}']
        else:
            df['ADX'] = df['DMP'] = df['DMN'] = np.nan

        # 8. StochRSI (Stochastic RSI) (v20.0.44追加機能)
        stochrsi_results = df.ta.stochrsi(
            length=STOCHRSI_LENGTH, 
            k=STOCHRSI_K, 
            d=STOCHRSI_D, 
            append=False
        )
        STOCHRSIS_COL = f'STOCHRSIS_{STOCHRSI_LENGTH}_{STOCHRSI_K}_{STOCHRSI_D}'
        STOCHRSID_COL = f'STOCHRSID_{STOCHRSI_LENGTH}_{STOCHRSI_K}_{STOCHRSI_D}'
        if STOCHRSIS_COL in stochrsi_results.columns:
            df['StochRSI_K'] = stochrsi_results[STOCHRSIS_COL]
            df['StochRSI_D'] = stochrsi_results[STOCHRSID_COL]
        else:
             df['StochRSI_K'] = df['StochRSI_D'] = np.nan


        # データフレームから最後の行のインジケーター値を抽出するために、列名を短縮
        df = df.rename(columns={
            f'SMA_{LONG_TERM_SMA_LENGTH}': 'SMA_200',
            f'ATR_{ATR_LENGTH}': 'ATR',
            f'RSI_{STOCHRSI_LENGTH}': 'RSI', # pandas_taのデフォルトRSIをStochRSIの長さで上書き
        }, errors='ignore')
        
        
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): インジケーター計算中に致命的なエラーが発生しました: {e}", exc_info=True)
        # エラー時に存在するべき列をNaNで初期化し、続行を試みる
        required_cols = ['MACD', 'MACD_S', 'MACD_H', 'RSI', 'BB_width', 'SMA_200', 'ATR', 'OBV', 'ADX', 'StochRSI_K']
        for col in required_cols:
            if col not in df.columns:
                df[col] = np.nan
        
    return df

def calculate_atr_sl_tp(latest_price: float, atr: float, side: str) -> Tuple[float, float, float]:
    """
    最新価格、ATR、サイドに基づき、SL、TPを計算する。
    """
    
    # 💡 リスク幅をATRに基づいて決定。ただし最低リスク幅を下回らない
    risk_by_atr = atr * ATR_MULTIPLIER_SL
    min_risk_usdt = latest_price * MIN_RISK_PERCENT
    risk_width = max(risk_by_atr, min_risk_usdt)
    
    reward_width = risk_width * RR_RATIO_TARGET # リスクリワード比率 (1:1.5)
    
    if side == 'long':
        stop_loss = latest_price - risk_width
        take_profit = latest_price + reward_width
    elif side == 'short':
        stop_loss = latest_price + risk_width
        take_profit = latest_price - reward_width
    else:
        return 0.0, 0.0, 0.0

    return stop_loss, take_profit, RR_RATIO_TARGET

def calculate_score(df: pd.DataFrame, timeframe: str, side: str, macro_context: Dict) -> Tuple[float, Dict]:
    """
    テクニカル指標に基づき、ロング/ショートの総合スコアを計算する (v20.0.46ロジック)
    """
    if df.empty or len(df) < 2:
        return 0.0, {}
        
    # 最新のインジケーター値と1つ前の値を抽出
    latest = df.iloc[-1]
    prev = df.iloc[-2]
    latest_close = latest['close']

    # 必須インジケーターのチェック (NaNを0として扱う)
    macd_h = latest.get('MACD_H', np.nan)
    macd = latest.get('MACD', np.nan)
    macd_s = latest.get('MACD_S', np.nan)
    rsi = latest.get('RSI', np.nan)
    sma_200 = latest.get('SMA_200', np.nan)
    bb_width = latest.get('BB_width', np.nan)
    adx = latest.get('ADX', np.nan)
    stochrsi_k = latest.get('StochRSI_K', np.nan)
    
    # NaNチェック
    if pd.isna(macd_h) or pd.isna(rsi) or pd.isna(sma_200) or pd.isna(bb_width) or pd.isna(adx) or pd.isna(stochrsi_k):
        return 0.0, {}
        
    total_score = BASE_SCORE # ベーススコア (0.40)
    tech_data = {'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 'indicators': latest.to_dict()}
    
    # 1. 長期トレンド (SMA 200) - 順張り/逆張りペナルティ
    trend_val = 0.0
    if side == 'long' and latest_close > sma_200:
        trend_val = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
    elif side == 'short' and latest_close < sma_200:
        trend_val = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
    elif (side == 'long' and latest_close < sma_200) or (side == 'short' and latest_close > sma_200):
        trend_val = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    total_score += trend_val
    tech_data['long_term_reversal_penalty_value'] = trend_val

    # 2. MACDモメンタム (MACDヒストグラムの方向)
    macd_val = 0.0
    if side == 'long' and macd_h > 0:
        macd_val = MACD_CROSS_PENALTY 
    elif side == 'short' and macd_h < 0:
        macd_val = MACD_CROSS_PENALTY
    elif (side == 'long' and macd_h < 0) or (side == 'short' and macd_h > 0):
        macd_val = -MACD_CROSS_PENALTY # MACDの方向と取引方向が不一致
    total_score += macd_val
    tech_data['macd_penalty_value'] = macd_val

    # 3. RSIモメンタム加速ボーナス
    rsi_val = 0.0
    if side == 'long' and rsi > RSI_MOMENTUM_LOW:
        rsi_val = (rsi - RSI_MOMENTUM_LOW) / (100 - RSI_MOMENTUM_LOW) * RSI_DIVERGENCE_BONUS
    elif side == 'short' and rsi < (100 - RSI_MOMENTUM_LOW):
        rsi_val = ((100 - RSI_MOMENTUM_LOW) - rsi) / (100 - RSI_MOMENTUM_LOW) * RSI_DIVERGENCE_BONUS
    total_score += rsi_val
    tech_data['rsi_momentum_bonus_value'] = rsi_val
    
    # 4. OBVモメンタム確証ボーナス
    obv_val = 0.0
    if side == 'long' and latest['OBV'] > prev['OBV']:
        obv_val = OBV_MOMENTUM_BONUS
    elif side == 'short' and latest['OBV'] < prev['OBV']:
        obv_val = OBV_MOMENTUM_BONUS
    total_score += obv_val
    tech_data['obv_momentum_bonus_value'] = obv_val

    # 5. ボラティリティ過熱ペナルティ (BB_widthが大きすぎる場合)
    vol_val = 0.0
    # BB幅が最新価格の一定割合 (VOLATILITY_BB_PENALTY_THRESHOLD = 1%) を超える場合
    if bb_width / latest_close > VOLATILITY_BB_PENALTY_THRESHOLD:
        vol_val = -0.10 # ペナルティ
    total_score += vol_val
    tech_data['volatility_penalty_value'] = vol_val

    # 6. 🆕 ADXトレンド確証ボーナス (v20.0.44)
    adx_bonus = 0.0
    if adx >= ADX_TREND_STRENGTH_THRESHOLD:
        if side == 'long' and latest.get('DMP', 0) > latest.get('DMN', 0):
            adx_bonus = ADX_TREND_BONUS 
        elif side == 'short' and latest.get('DMP', 0) < latest.get('DMN', 0):
            adx_bonus = ADX_TREND_BONUS
    total_score += adx_bonus
    tech_data['adx_trend_bonus_value'] = adx_bonus
    tech_data['adx_raw_value'] = adx

    # 7. 🆕 StochRSI過熱ペナルティ/回復ボーナス (v20.0.44)
    stoch_penalty = 0.0
    # StochRSIが極端な水準にある場合はペナルティ（逆張り防止）
    if side == 'long' and stochrsi_k > (100 - STOCHRSI_BOS_LEVEL): # 80超えで買われ過ぎ
        stoch_penalty = -STOCHRSI_BOS_PENALTY
    elif side == 'short' and stochrsi_k < STOCHRSI_BOS_LEVEL: # 20未満で売られ過ぎ
        stoch_penalty = -STOCHRSI_BOS_PENALTY
    # StochRSIが極端な水準から回復する兆候があればボーナス
    elif side == 'long' and stochrsi_k < STOCHRSI_BOS_LEVEL and prev.get('StochRSI_K', 0) <= stochrsi_k:
        # 売られ過ぎから反転する兆候
        stoch_penalty = STOCHRSI_BOS_PENALTY / 2.0
    elif side == 'short' and stochrsi_k > (100 - STOCHRSI_BOS_LEVEL) and prev.get('StochRSI_K', 100) >= stochrsi_k:
        # 買われ過ぎから反転する兆候
        stoch_penalty = STOCHRSI_BOS_PENALTY / 2.0
        
    total_score += stoch_penalty
    tech_data['stoch_rsi_penalty_value'] = stoch_penalty
    tech_data['stoch_rsi_k_value'] = stochrsi_k

    # 8. マクロ環境と流動性ボーナス
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    # FGIプロキシをスコアに変換し加算 (最大 FGI_PROXY_BONUS_MAX)
    fgi_bonus = min(abs(fgi_proxy) * 0.5, FGI_PROXY_BONUS_MAX) # FGIの勢いをボーナスに
    total_score += fgi_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus
    
    # 流動性ボーナス (トップ銘柄はボーナス) - symbolがTOP_SYMBOL_LIMITに含まれるかどうかのプロキシ
    if latest['symbol'] in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]:
        liq_bonus = LIQUIDITY_BONUS_MAX
    else:
        liq_bonus = 0.0
    total_score += liq_bonus
    tech_data['liquidity_bonus_value'] = liq_bonus

    # 最終的なスコア
    final_score = total_score + STRUCTURAL_PIVOT_BONUS # 構造的ベース優位性 (0.05)

    # スコアを [0.0, 1.0] の範囲にクリッピング
    final_score = max(0.0, min(1.0, final_score))
    
    return final_score, tech_data

def determine_entry_signal(analyzed_data: List[Dict]) -> Optional[Dict]:
    """
    分析結果リストから、現在の市場環境に適した最も強力な取引シグナルを一つだけ選択する。
    """
    global LAST_SIGNAL_TIME, TRADE_SIGNAL_COOLDOWN, TOP_SIGNAL_COUNT, GLOBAL_MACRO_CONTEXT
    
    current_time = time.time()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 1. 閾値以上のスコアを持つシグナルを抽出
    eligible_signals = [
        s for s in analyzed_data 
        if s.get('score', 0.0) >= current_threshold
    ]
    
    if not eligible_signals:
        logging.info(f"ℹ️ {len(analyzed_data)}件の分析結果中、取引閾値 ({current_threshold*100:.0f}点) を超えるシグナルはありませんでした。")
        return None

    # 2. スコア順にソート
    sorted_signals = sorted(eligible_signals, key=lambda x: x.get('score', 0.0), reverse=True)
    
    # 3. クールダウン期間を考慮し、最も強力なシグナルを選択
    best_signal: Optional[Dict] = None
    
    for signal in sorted_signals[:TOP_SIGNAL_COUNT]: # TOP_SIGNAL_COUNT (3件) のみ考慮
        symbol = signal['symbol']
        timeframe = signal['timeframe']
        
        # 既にポジションを持っている銘柄はスキップ
        if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
            logging.info(f"ℹ️ {symbol} ({timeframe}): 既にポジション管理中。シグナルをスキップします。")
            continue
            
        # クールダウン期間のチェック (同じシンボル、同じ方向)
        signal_key = f"{symbol}_{signal['side']}"
        last_signal_time = LAST_SIGNAL_TIME.get(signal_key, 0.0)
        
        if current_time - last_signal_time < TRADE_SIGNAL_COOLDOWN:
            cooldown_remaining = timedelta(seconds=TRADE_SIGNAL_COOLDOWN - (current_time - last_signal_time))
            logging.info(f"ℹ️ {symbol} ({signal['side']}): クールダウン期間中。スキップします。 (残り: {str(cooldown_remaining).split('.')[0]})")
            continue
            
        # 最初の有効なシグナルを最良とする
        best_signal = signal
        break # 最もスコアの高い、有効なシグナルが見つかったらループを抜ける
    
    if best_signal:
        signal_key = f"{best_signal['symbol']}_{best_signal['side']}"
        LAST_SIGNAL_TIME[signal_key] = current_time # シグナル時間を更新
        logging.info(f"🔥 取引シグナル確定: {best_signal['symbol']} ({best_signal['side']}) Score: {best_signal['score']:.2f} @ {best_signal['timeframe']}")

    return best_signal

async def execute_trade(signal: Dict, current_balance: float) -> Dict:
    """
    取引シグナルに基づき、取引所APIを介して成行注文、SL/TP設定、ポジション管理を行う。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS, TEST_MODE, LEVERAGE
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    rr_ratio = signal['rr_ratio']
    
    if not IS_CLIENT_READY or TEST_MODE:
        logging.warning("⚠️ TEST_MODEまたはクライアント未準備。取引をスキップします。")
        return {'status': 'ok', 'filled_usdt': FIXED_NOTIONAL_USDT, 'filled_amount': 0.0, 'is_test': True}
        
    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'CCXT Client not initialized'}
    
    # 1. 注文数量の計算 (固定ロット)
    # ノミナルバリュー (USDT) / 現在価格 = ベース通貨数量
    amount_base_currency = FIXED_NOTIONAL_USDT / entry_price 
    
    # 2. 注文方向の決定
    order_side = 'buy' if side == 'long' else 'sell'
    # ポジション方向の決定（ロングを増やすのは'buy'、ショートを増やすのは'sell'）
    position_side = 'long' if side == 'long' else 'short'
    
    # 3. 注文の実行
    trade_result = {'status': 'error', 'error_message': 'Unknown execution error'}
    
    try:
        # 💡 成行注文 (Market Order) を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            'market', 
            order_side, 
            amount_base_currency,
            params={
                'positionSide': position_side.capitalize() if EXCHANGE_CLIENT.id == 'binance' else position_side # 取引所によって異なる
            }
        )
        
        # 約定詳細の確認
        filled_amount = float(order.get('filled', 0.0))
        filled_price = float(order.get('price', entry_price)) # 注文価格 (成行なので約定価格と多少異なる)
        filled_usdt = filled_amount * filled_price
        
        # 注文が成功し、数量が0より大きいことを確認
        if filled_amount > 0 and filled_usdt > 0:
            
            # 4. ポジション情報を作成し、グローバルリストに追加
            liquidation_price = calculate_liquidation_price(filled_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
            
            position = {
                'id': str(uuid.uuid4()), # ユニークなID
                'symbol': symbol,
                'side': side,
                'entry_price': filled_price,
                'contracts': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'liquidation_price': liquidation_price,
                'entry_time': time.time(),
                'rr_ratio': rr_ratio
            }
            OPEN_POSITIONS.append(position)
            
            # 5. 取引所側でSL/TP注文を設定（取引所が対応している場合）
            # ここではccxtのUnified Margin/Isolated MarginのTP/SL機能は使用しない
            # ロジック側 (check_and_manage_open_positions) で監視するため、ここではスキップ
            
            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_price': filled_price,
                'filled_usdt': filled_usdt,
            }
            logging.info(f"✅ 取引成功: {symbol} ({side}) {filled_amount:.4f} @ {filled_price:.4f}。ポジション管理リストに追加。")
            
        else:
            trade_result = {'status': 'error', 'error_message': f'注文は成功したが約定数量が0: {order}'}

    except ccxt.InsufficientFunds as e:
        trade_result = {'status': 'error', 'error_message': f'資金不足エラー: {e.args[0]}'}
        logging.error(f"❌ 取引失敗 (資金不足): {e}")
    except ccxt.InvalidOrder as e:
        # Amount can not be less than zero などのエラー対策
        trade_result = {'status': 'error', 'error_message': f'無効な注文エラー: {e.args[0]}'}
        logging.error(f"❌ 取引失敗 (無効な注文): {e}")
    except Exception as e:
        trade_result = {'status': 'error', 'error_message': f'API実行中に予期せぬエラー: {e}'}
        logging.error(f"❌ 取引失敗 (APIエラー): {e}", exc_info=True)
        
    return trade_result

async def check_and_manage_open_positions():
    """
    オープンポジションをチェックし、SL/TPに達した場合は決済注文を実行する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not OPEN_POSITIONS or not IS_CLIENT_READY:
        return
        
    logging.info(f"⏳ {len(OPEN_POSITIONS)}件のオープンポジションを監視中...")
    
    # 1. 全ポジションの現在価格を効率的に取得
    symbols_to_fetch = list(set([p['symbol'] for p in OPEN_POSITIONS]))
    current_prices: Dict[str, float] = {}
    
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        for symbol in symbols_to_fetch:
            if symbol in tickers and tickers[symbol]['last'] is not None:
                current_prices[symbol] = tickers[symbol]['last']
    except Exception as e:
        logging.error(f"❌ ポジション監視中の現在価格取得エラー: {e}", exc_info=True)
        return

    positions_to_close: List[Tuple[Dict, str]] = [] # (position_data, exit_type)
    
    # 2. SL/TP条件のチェック
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        current_price = current_prices.get(symbol, 0.0)
        
        if current_price == 0.0:
            logging.warning(f"⚠️ {symbol}: 現在価格が取得できなかったため、チェックをスキップします。")
            continue
            
        is_sl_hit = False
        is_tp_hit = False
        
        if pos['side'] == 'long':
            # ロングの場合: SLは価格が下落、TPは価格が上昇
            if current_price <= pos['stop_loss']:
                is_sl_hit = True
            elif current_price >= pos['take_profit']:
                is_tp_hit = True
                
        elif pos['side'] == 'short':
            # ショートの場合: SLは価格が上昇、TPは価格が下落
            if current_price >= pos['stop_loss']:
                is_sl_hit = True
            elif current_price <= pos['take_profit']:
                is_tp_hit = True
                
        # 清算価格チェック (最後のセーフティネット)
        is_liquidation_imminent = False
        if pos['liquidation_price'] > 0:
             if pos['side'] == 'long' and current_price <= pos['liquidation_price'] * 1.0005: # 清算価格のわずか上
                is_liquidation_imminent = True
             elif pos['side'] == 'short' and current_price >= pos['liquidation_price'] * 0.9995: # 清算価格のわずか下
                is_liquidation_imminent = True

        if is_sl_hit or is_tp_hit or is_liquidation_imminent:
            exit_type = "SLトリガー (損切り)" if is_sl_hit else ("TPトリガー (利確)" if is_tp_hit else "清算価格間近")
            positions_to_close.append((pos, exit_type))
            
    # 3. 決済処理の実行
    new_open_positions: List[Dict] = []
    
    for pos, exit_type in positions_to_close:
        symbol = pos['symbol']
        contracts = pos['contracts']
        side = pos['side']
        current_price = current_prices.get(symbol, pos['entry_price']) # 安全のため、取得できない場合はエントリー価格
        
        # 注文方向: ロングを決済 = 'sell'、ショートを決済 = 'buy'
        close_order_side = 'sell' if side == 'long' else 'buy'
        
        exit_result = {'status': 'error', 'error_message': 'Unknown exit error', 'pnl_usdt': 0.0, 'pnl_rate': 0.0}
        
        try:
            if not TEST_MODE:
                # 💡 ポジション決済の成行注文を実行
                order = await EXCHANGE_CLIENT.create_order(
                    symbol, 
                    'market', 
                    close_order_side, 
                    contracts, 
                    params={
                        'positionSide': side.capitalize() if EXCHANGE_CLIENT.id == 'binance' else side 
                    }
                )
                
                # 約定価格はオーダー情報から取得、または現在の価格を使用
                exit_price = float(order.get('price', current_price))
            else:
                # テストモードではシミュレーション
                exit_price = current_price

            # P&L計算
            entry_price = pos['entry_price']
            filled_usdt = pos['filled_usdt']
            
            if side == 'long':
                pnl_rate_unleveraged = (exit_price - entry_price) / entry_price
            else: # short
                pnl_rate_unleveraged = (entry_price - exit_price) / entry_price
                
            pnl_rate = pnl_rate_unleveraged * LEVERAGE
            pnl_usdt = filled_usdt * pnl_rate_unleveraged # P&Lは名目価値に非レバレッジ率をかける
            
            exit_result = {
                'status': 'ok',
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'filled_amount': contracts,
                'exit_type': exit_type,
            }

            log_signal(pos, "ポジション決済", exit_result)
            
            # Telegram通知
            await send_telegram_notification(format_telegram_message(pos, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), exit_result, exit_type))

        except Exception as e:
            logging.error(f"❌ {symbol} ({side}) 決済注文中にエラー: {e}", exc_info=True)
            # 決済エラーの場合、次のチェックで再度試行するためにポジションをリストに残す
            new_open_positions.append(pos)
            continue

    # 決済されなかったポジションを新しいリストに保持
    positions_closed_ids = [p['id'] for p, _ in positions_to_close]
    for pos in OPEN_POSITIONS:
        if pos['id'] not in positions_closed_ids:
            new_open_positions.append(pos)
            
    OPEN_POSITIONS = new_open_positions
    logging.info(f"✅ ポジション監視完了。残り管理中ポジション数: {len(OPEN_POSITIONS)}")

# ====================================================================================
# MAIN SCHEDULERS
# ====================================================================================

async def main_bot_scheduler():
    """
    BOTのメインロジック (分析、シグナル生成、取引実行) を定期的に実行する
    """
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, LAST_WEBSHARE_UPLOAD_TIME, LAST_SUCCESS_TIME # 🚨 FIX: UnboundLocalError対策
    
    # 1. クライアントの初期化
    if not await initialize_exchange_client():
        logging.critical("🚨 クライアントの初期化に失敗しました。BOTを停止します。")
        return
        
    # 2. 初期口座ステータスとマクロ情報を取得
    account_status = await get_account_status()
    await fetch_fgi_data()
    
    # 3. 監視シンボルリストの初期ロード
    CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols(TOP_SYMBOL_LIMIT)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 4. 起動完了通知
    await send_telegram_notification(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold))
    
    # ----------------------------------------------------------------------
    # メインループ
    # ----------------------------------------------------------------------
    logging.info("⏳ メイン分析スケジューラを開始します。")
    while True:
        start_time = time.time()
        
        try:
            # 1. 市場環境と口座情報の更新 (一定間隔で)
            await fetch_fgi_data()
            account_status = await get_account_status()
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

            if not SKIP_MARKET_UPDATE and (start_time - LAST_SUCCESS_TIME) > 60 * 10: 
                # 10分ごとに監視銘柄リストを更新 (API負荷軽減のため)
                CURRENT_MONITOR_SYMBOLS = await get_top_volume_symbols(TOP_SYMBOL_LIMIT)
                LAST_SUCCESS_TIME = start_time
                
            analyzed_signals: List[Dict] = []
            
            # 2. 全てのターゲット銘柄・タイムフレームで分析を実行
            for symbol in CURRENT_MONITOR_SYMBOLS:
                
                # APIレート制限対策
                await asyncio.sleep(0.1) 
                
                for timeframe in TARGET_TIMEFRAMES:
                    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
                    df = await fetch_ohlcv_data(symbol, timeframe, limit)
                    
                    if df.empty or len(df) < limit:
                        logging.warning(f"⚠️ {symbol} ({timeframe}): 必要なデータ ({limit}本) が不足しているためスキップします。")
                        continue
                        
                    df_tech = calculate_technical_indicators(df)
                    
                    # ロング/ショート両方のスコアを計算
                    for side in ['long', 'short']:
                        score, tech_data = calculate_score(df_tech, timeframe, side, GLOBAL_MACRO_CONTEXT)
                        
                        signal = {
                            'symbol': symbol,
                            'timeframe': timeframe,
                            'side': side,
                            'score': score,
                            'tech_data': tech_data,
                            'latest_close': df_tech.iloc[-1]['close']
                        }
                        analyzed_signals.append(signal)

            # 3. 分析結果の格納
            LAST_ANALYSIS_SIGNALS = analyzed_signals
            
            # 4. 最強シグナルの決定
            # ATR/SL/TPの計算をスコアリング後に実行
            for signal in LAST_ANALYSIS_SIGNALS:
                latest_close = signal['latest_close']
                atr = signal['tech_data']['indicators'].get('ATR', np.nan)
                
                if pd.notna(atr) and atr > 0:
                    sl, tp, rr = calculate_atr_sl_tp(latest_close, atr, signal['side'])
                    signal['entry_price'] = latest_close
                    signal['stop_loss'] = sl
                    signal['take_profit'] = tp
                    signal['rr_ratio'] = rr
                    signal['liquidation_price'] = calculate_liquidation_price(latest_close, LEVERAGE, signal['side'], MIN_MAINTENANCE_MARGIN_RATE)
                else:
                    signal['entry_price'] = latest_close
                    signal['stop_loss'] = 0.0
                    signal['take_profit'] = 0.0
                    signal['rr_ratio'] = 0.0
                    signal['liquidation_price'] = 0.0
                    signal['score'] = 0.0 # ATRが計算できない場合はスコア無効
                    
            best_signal = determine_entry_signal(LAST_ANALYSIS_SIGNALS)
            
            # 5. 取引実行
            if best_signal and best_signal['stop_loss'] > 0.0 and not TEST_MODE:
                trade_result = await execute_trade(best_signal, ACCOUNT_EQUITY_USDT)
                
                # 通知とログ
                log_signal(best_signal, "取引シグナル", trade_result)
                await send_telegram_notification(format_telegram_message(best_signal, "取引シグナル", current_threshold, trade_result))

            # 6. 定時レポートの送信 (取引閾値未満の最高スコア)
            await notify_highest_analysis_score()

            # 7. WebShareのデータ更新 (1時間ごと)
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                webshare_data = {
                    'version': BOT_VERSION,
                    'timestamp': datetime.now(JST).isoformat(),
                    'account': {
                        'equity_usdt': ACCOUNT_EQUITY_USDT,
                        'open_positions_count': len(OPEN_POSITIONS),
                    },
                    'macro': GLOBAL_MACRO_CONTEXT,
                    'top_signals': sorted(
                        [s for s in LAST_ANALYSIS_SIGNALS if s['score'] > BASE_SCORE], 
                        key=lambda x: x.get('score', 0.0), reverse=True
                    )[:10], # トップ10の分析結果を送信
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
                
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
        except Exception as e:
            logging.critical(f"❌ メインボットスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時はTelegramに通知
            await send_telegram_notification(f"🚨 <b>メインBOTスケジューラで致命的エラー</b>\n<code>{e}</code>\n次のループで再試行します。")

        # 8. 次の実行まで待機
        elapsed_time = time.time() - start_time
        sleep_duration = max(0, LOOP_INTERVAL - elapsed_time)
        logging.info(f"💤 メイン分析ループ待機中... (所要時間: {elapsed_time:.2f}秒, 待機時間: {sleep_duration:.2f}秒)")
        await asyncio.sleep(sleep_duration)


async def position_monitor_scheduler():
    """
    オープンポジションのSL/TPチェックを定期的に実行する
    """
    # メインループの完了を待機
    while not IS_FIRST_MAIN_LOOP_COMPLETED:
        logging.info("ℹ️ メイン分析ループの初回完了を待機中...")
        await asyncio.sleep(5) 
    
    if not IS_CLIENT_READY and not TEST_MODE:
         logging.critical("🚨 クライアントが未準備のため、ポジション監視スケジューラをスキップします。")
         return
         
    logging.info("⏳ ポジション監視スケジューラを開始します。")
    
    while True:
        try:
            # ポジションのSL/TPチェックと決済
            await check_and_manage_open_positions()
            
            # 次のチェックまで待機
            await asyncio.sleep(MONITOR_INTERVAL)
            
        except Exception as e:
            logging.error(f"❌ ポジション監視スケジューラでエラーが発生しました: {e}", exc_info=True)
            # エラーが発生しても監視は続ける
            await asyncio.sleep(MONITOR_INTERVAL)


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
    """捕捉されなかった例外を処理し、JSONレスポンスを返す"""
    error_message = f"Internal Server Error: {exc.__class__.__name__}: {exc}"
    logging.error(f"🚨 Unhandled Exception: {error_message}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )


if __name__ == "__main__":
    # 環境変数からポートを取得、なければ8000を使用
    port = int(os.environ.get("PORT", 8000))
    # uvicornサーバーを起動
    uvicorn.run(app, host="0.0.0.0", port=port)
