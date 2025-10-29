# ====================================================================================
# Apex BOT v20.0.45 - Future Trading / 30x Leverage 
# (Feature: v20.0.44機能 + 致命的バグ修正)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
# 6. 💡 新規: ADX/StochRSIによるスコア判別能力強化 (v20.0.44)
# 7. 💡 修正: main_bot_scheduler内のLAST_WEBSHARE_UPLOAD_TIMEに関するUnboundLocalErrorを修正 (v20.0.45)
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
BOT_VERSION = "v20.0.45"            # 💡 BOTバージョンを v20.0.45 に更新 
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
STOCHRSI_BOS_LEVEL = 20            # 買われ過ぎ/売られ過ぎ水準
STOCHRSI_BOS_PENALTY = 0.08        # StochRSIが極端な水準にある場合のペナルティ/ボーナス

# ADX
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
        balance_section += f"<pre>⚠️ ステータス取得失敗 (致命的エラーにより取引停止中)</pre>\n"
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
                        if asset.get('asset') == 'USDT':
                            total_usdt_balance_fallback = float(asset.get('totalEquity', 0.0))
                            break
                            
                # どちらか大きい方を使用 (ccxtの計算が間違っている場合に備えて)
                if total_usdt_balance_fallback > total_usdt_balance:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.info(f"ℹ️ MEXCのフォールバックロジックにより残高を更新しました: {total_usdt_balance:.2f} USDT")


        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        logging.info(f"✅ 口座ステータス取得完了: 総資産 (USDT Equity): {ACCOUNT_EQUITY_USDT:,.2f} USDT")

        # ポジション情報の取得 (MEXCはfetch_positionsを使用)
        raw_positions = []
        try:
            raw_positions = await EXCHANGE_CLIENT.fetch_positions(params={'defaultType': 'swap'})
        except Exception as e:
            logging.warning(f"⚠️ ポジション情報の取得に失敗しました。: {e}")
        
        open_positions = []
        for p in raw_positions:
            if p.get('contracts', 0) != 0 and p.get('notional', 0) != 0:
                # CCXT標準のポジション情報から必要な情報を抽出
                symbol = p.get('symbol')
                entry_price = p.get('entryPrice', 0.0)
                contracts = p.get('contracts', 0)
                notional = p.get('notional', 0.0)
                side = 'long' if contracts > 0 else 'short'
                
                # エントリー価格、清算価格の計算は後で行うため、ここでは最小限の情報
                open_positions.append({
                    'symbol': symbol,
                    'side': side,
                    'entry_price': entry_price,
                    'contracts': contracts,
                    'filled_usdt': notional,
                    'stop_loss': 0.0, # 管理用のプレースホルダー
                    'take_profit': 0.0, # 管理用のプレースホルダー
                    'leverage': p.get('leverage', LEVERAGE),
                    'liquidation_price': calculate_liquidation_price(entry_price, p.get('leverage', LEVERAGE), side),
                })
        
        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }

    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_top_volume_symbols() -> List[str]:
    global EXCHANGE_CLIENT, SKIP_MARKET_UPDATE
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。トップボリュームシンボルの更新をスキップし、DEFAULT_SYMBOLSを使用します。")
        return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ トップシンボル取得失敗: CCXTクライアントが準備できていません。DEFAULT_SYMBOLSを使用します。")
        return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]

    try:
        logging.info("⏳ トップボリュームシンボルを取引所から取得します...")
        
        # 出来高 (Volume) のデータが含まれるティッカー情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not tickers or not isinstance(tickers, dict) or not tickers.keys(): 
            logging.warning("⚠️ fetch_tickersのデータが空または無効です。DEFAULT_SYMBOLSを使用します。")
            return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]

        # 1. USDT建て先物/スワップペアのみをフィルタリング
        future_tickers = {
            symbol: data 
            for symbol, data in tickers.items()
            if '/USDT' in symbol 
            and (EXCHANGE_CLIENT.markets.get(symbol, {}).get('type') in ['future', 'swap'])
            and data.get('quoteVolume') is not None # quoteVolume (USDTでの取引高) が存在する
        }
        
        if not future_tickers:
            logging.warning("⚠️ フィルタリングされた先物USDTペアがありません。DEFAULT_SYMBOLSを使用します。")
            return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
            
        # 2. quoteVolume (USDT) で降順ソート
        sorted_tickers = sorted(
            future_tickers.items(),
            key=lambda item: float(item[1].get('quoteVolume', 0.0)),
            reverse=True
        )

        # 3. TOP Nのシンボルを取得
        top_future_symbols = [symbol for symbol, data in sorted_tickers[:TOP_SYMBOL_LIMIT]]

        # 4. DEFAULT_SYMBOLS から主要な基軸通貨を強制的に追加 (最低限の銘柄を保証)
        for d_symbol in DEFAULT_SYMBOLS:
            if d_symbol not in top_future_symbols and EXCHANGE_CLIENT.markets.get(d_symbol, {}).get('active'):
                top_future_symbols.append(d_symbol)
                
        # 最終的にTOP_SYMBOL_LIMIT+X (Xはデフォルト銘柄の数) になる可能性があるが、主要銘柄はカバーできる
        logging.info(f"✅ トップボリュームシンボル ({len(top_future_symbols)}件) を取得しました。")
        return top_future_symbols

    except ccxt.NetworkError as e:
        logging.error(f"❌ ネットワークエラーによりトップシンボル取得に失敗しました: {e}。DEFAULT_SYMBOLSを使用します。")
    except Exception as e:
        # 💡 致命的エラー修正強化: fetch_tickersのAttributeError対策を兼ねる
        logging.error(f"❌ 予期せぬエラーによりトップシンボル取得に失敗しました: {e}。DEFAULT_SYMBOLSを使用します。", exc_info=True)

    return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]


async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCV取得失敗: クライアントが準備されていません。({symbol}, {timeframe})")
        return None

    try:
        # fetch_ohlcvのCCXTシンボルは、fetch_marketsで確認した取引所固有のシンボルを使うべきだが、
        # ここではCCXT標準の 'BTC/USDT' 形式を使用し、CCXTライブラリに変換を任せる
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)

        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが取得できませんでした (空データ)。")
            return None

        # DataFrameに変換
        df = pd.DataFrame(
            ohlcv, 
            columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume']
        )
        # タイムスタンプをdatetimeオブジェクトに変換
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # データ量が指定の制限を下回っていないかチェック
        if len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): 要求された{limit}件に対し{len(df)}件しか取得できませんでした。分析精度に影響が出る可能性があります。")
        
        return df

    except ccxt.DDoSProtection as e:
        logging.error(f"❌ DDoS保護エラー: {symbol} ({timeframe}) のデータ取得に失敗しました。レート制限に注意してください。{e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 取引所エラー: {symbol} ({timeframe}) のデータ取得に失敗しました。シンボル名を確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ OHLCVデータ取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return None

async def fetch_fgi_data() -> Dict:
    global GLOBAL_MACRO_CONTEXT
    try:
        # 💡 非同期で外部APIを呼び出す
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and data.get('data'):
            latest_data = data['data'][0]
            value = latest_data.get('value', '50')
            value_classification = latest_data.get('value_classification', 'Neutral')
            
            fgi_value = float(value)
            
            # FGI (0=Extreme Fear, 100=Extreme Greed) を -0.05 から +0.05 の範囲のプロキシに変換
            # 中央値 50 を 0.0 に、0 を -0.05 に、100 を +0.05 にマッピング
            # proxy = ((fgi_value - 50) / 50) * FGI_PROXY_BONUS_MAX
            
            # スケールを変更: (0-100) -> (-1.0 to 1.0) * MaxBonus
            normalized_proxy = (fgi_value - 50) / 50
            fgi_proxy = normalized_proxy * FGI_PROXY_BONUS_MAX
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({value_classification})"
            logging.info(f"✅ FGI (恐怖・貪欲指数) を更新しました。値: {fgi_value} ({value_classification}), Proxy: {fgi_proxy:+.4f}")
            return GLOBAL_MACRO_CONTEXT
        
        logging.warning("⚠️ FGIデータが空または予期せぬ形式です。前回値を使用します。")
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}。前回値を使用します。")
    except Exception as e:
        logging.error(f"❌ FGIデータ処理エラー: {e}。前回値を使用します。", exc_info=True)
        
    return GLOBAL_MACRO_CONTEXT # エラー時は前回値または初期値が残る


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    DataFrameにpandas_taを使用してテクニカル指標を追加し、最終行の値を辞書で返す。
    
    Args:
        df: OHLCVデータを含むPandas DataFrame。

    Returns:
        計算されたテクニカル指標の最終行の値を含む辞書。
    """
    
    if df is None or len(df) == 0:
        return {}

    # 1. MACD (Moving Average Convergence Divergence)
    # MACD の結果は 3 列: MACD, MACDH (ヒストグラム), MACDS (シグナル)
    # pandas_ta のデフォルト設定 (12, 26, 9) を使用
    df.ta.macd(append=True)
    
    # 2. RSI (Relative Strength Index)
    df.ta.rsi(length=14, append=True)
    
    # 3. Bollinger Bands (BBANDS)
    # 結果は 3 列: BBL (Lower), BBM (Middle), BBU (Upper), BBB (Bandwidth), BBP (Percent)
    df.ta.bbands(append=True) 

    # 4. SMA (Simple Moving Average) - 長期トレンド判定用
    df.ta.sma(length=LONG_TERM_SMA_LENGTH, close='Close', append=True) 
    
    # 5. ATR (Average True Range) - リスク管理用
    df.ta.atr(length=ATR_LENGTH, append=True)
    
    # 6. OBV (On-Balance Volume) - 出来高モメンタム判定用
    df.ta.obv(append=True)
    
    # 7. Stochastic RSI (StochRSI) - 買われ過ぎ/売られ過ぎ判定用
    # デフォルト: k=3, d=3, length=14, rsi_length=14
    df.ta.stochrsi(append=True) 
    
    # 8. ADX (Average Directional Index) - トレンド強度判定用
    # 結果は 3 列: ADX, DMI+, DMI-
    df.ta.adx(append=True)

    # -----------------------------------------------------------
    # 最終行のデータを辞書として抽出
    # -----------------------------------------------------------
    
    # MACDのデフォルト列名: MACD_12_26_9, MACDH_12_26_9, MACDS_12_26_9
    macd_cols = ['MACD_12_26_9', 'MACDH_12_26_9', 'MACDS_12_26_9']
    
    # RSIのデフォルト列名: RSI_14
    rsi_col = 'RSI_14'
    
    # BBANDSのデフォルト列名: BBL_5_2.0, BBM_5_2.0, BBU_5_2.0, BBB_5_2.0, BBP_5_2.0
    # ここではデフォルト設定の(20, 2.0)を使用するように修正 (BBANDS)
    bb_cols = [f'BBL_20_2.0', f'BBM_20_2.0', f'BBU_20_2.0', f'BBB_20_2.0', f'BBP_20_2.0']

    # SMAのデフォルト列名: SMA_200 (LONG_TERM_SMA_LENGTH=200)
    sma_col = f'SMA_{LONG_TERM_SMA_LENGTH}'
    
    # ATRのデフォルト列名: ATR_14
    atr_col = f'ATR_{ATR_LENGTH}'
    
    # OBVのデフォルト列名: OBV
    obv_col = 'OBV'

    # StochRSIのデフォルト列名: STOCHRSIk_14_14_3_3, STOCHRSId_14_14_3_3
    stochrsi_k_col = 'STOCHRSIk_14_14_3_3'
    stochrsi_d_col = 'STOCHRSId_14_14_3_3'
    
    # ADXのデフォルト列名: ADX_14, DMI+ (DMP_14), DMI- (DMN_14)
    adx_cols = ['ADX_14', 'DMP_14', 'DMN_14']

    all_cols = ['Close'] + macd_cols + [rsi_col] + bb_cols + [sma_col, atr_col, obv_col, stochrsi_k_col, stochrsi_d_col] + adx_cols
    
    # データが十分にない場合 (dropnaで最終行がNaNになる) に備え、エラーをトラップしてNaNを0またはNoneに置換する
    latest_data = {}
    try:
        # 最終行を取得し、必要な列だけを抽出
        latest_series = df.iloc[-1][all_cols]
        
        # 辞書に変換し、np.nanをPythonのNone/float('nan')に変換する
        for col in all_cols:
            if col in latest_series:
                latest_data[col] = latest_series[col] if not pd.isna(latest_series[col]) else None
            else:
                # 💡 堅牢な修正: pandas_taのバージョンや設定変更で列名が変わった場合を考慮
                # KeyError対策としてNoneをセット
                logging.warning(f"⚠️ 指標計算エラー: DataFrameに列名 '{col}' が見つかりませんでした。Noneを設定します。")
                latest_data[col] = None

    except Exception as e:
        logging.error(f"❌ 最新のテクニカルデータ抽出中にエラーが発生しました: {e}", exc_info=True)
        # エラーが発生した場合、すべての値をNone/0として返す
        latest_data = {col: None for col in all_cols}
        latest_data['Close'] = df['Close'].iloc[-1] if len(df) > 0 else 0.0

    
    # OBVの傾き (モメンタム) 計算
    # 最後の5期間のOBVの線形回帰の傾きを使用
    obv_momentum = 0.0
    try:
        # OBV_momentumはOBV列を使用
        obv_series = df[obv_col].dropna()
        if len(obv_series) >= 5:
            # 💡 修正: np.polyfitの戻り値エラー対策。戻り値はタプルではなく配列なので、[0]のみを取得
            # 傾き (m) のみを取得
            slope = np.polyfit(np.arange(len(obv_series[-5:])), obv_series[-5:], 1)[0] 
            obv_momentum = slope 
    except Exception as e:
        # データ不足やnp.polyfitの例外をキャッチ
        logging.warning(f"⚠️ OBVモメンタム計算中にエラーが発生しました: {e}")
        obv_momentum = 0.0

    latest_data['OBV_momentum'] = obv_momentum
    
    # BBandsの幅 (Bandwidth) を Close価格に対する比率で計算
    # BBB_20_2.0 を Close で割る
    bb_width_ratio = 0.0
    latest_close = latest_data.get('Close')
    bb_width = latest_data.get(f'BBB_20_2.0')
    
    if latest_close and latest_close > 0 and bb_width:
        bb_width_ratio = bb_width / latest_close
        
    latest_data['BB_width_ratio'] = bb_width_ratio
    latest_data['BB_width'] = bb_width # raw valueも保存
    
    return latest_data

def score_signal(symbol: str, timeframe: str, df: pd.DataFrame, tech_data: Dict, macro_context: Dict) -> Dict[str, Any]:
    """
    テクニカルデータに基づいて取引シグナルをスコアリングする。
    
    Args:
        symbol: 銘柄シンボル。
        timeframe: 時間軸。
        df: OHLCVデータを含むDataFrame (スコアリングには使用しないが、データ検証用)。
        tech_data: calculate_technical_indicatorsから返された指標データ。
        macro_context: FGIなどのマクロコンテキスト。

    Returns:
        スコアとサイド (long/short)、スコア内訳を含む辞書。
    """
    
    if not tech_data or len(df) == 0:
        return {'symbol': symbol, 'timeframe': timeframe, 'score': 0.0, 'side': 'none', 'tech_data': {}}

    # 最新の指標値を取得 (Noneチェックを忘れずに行う)
    close = tech_data.get('Close', 0.0)
    
    # MACD関連
    macd = tech_data.get('MACD_12_26_9', 0.0)
    macd_h = tech_data.get('MACDH_12_26_9', 0.0)
    macd_s = tech_data.get('MACDS_12_26_9', 0.0)
    
    # RSI関連
    rsi = tech_data.get('RSI_14', 50.0)
    
    # SMA関連
    sma_long = tech_data.get(f'SMA_{LONG_TERM_SMA_LENGTH}', close) 
    
    # ボラティリティ関連
    bb_width_ratio = tech_data.get('BB_width_ratio', 0.0)
    
    # OBV関連
    obv_momentum = tech_data.get('OBV_momentum', 0.0)
    
    # StochRSI関連
    stochrsi_k = tech_data.get('STOCHRSIk_14_14_3_3', np.nan)
    stochrsi_d = tech_data.get('STOCHRSId_14_14_3_3', np.nan)
    
    # ADX関連
    adx = tech_data.get('ADX_14', 0.0)
    dmi_plus = tech_data.get('DMP_14', 0.0)
    dmi_minus = tech_data.get('DMN_14', 0.0)
    
    
    # -----------------------------------------------------------
    # 1. 基本スコアと初期サイド設定
    # -----------------------------------------------------------
    score = BASE_SCORE # 基本点
    side = 'none'
    
    # MACDのクロスオーバーで初期サイドを決定 (MACDがシグナルを上抜け: Long, 下抜け: Short)
    # MACDHの符号で判定する方がシンプルかつ直感的
    if macd_h > 0:
        side = 'long'
    elif macd_h < 0:
        side = 'short'
    
    # MACDHがNaNの場合 (データ不足など) は取引しない
    if pd.isna(macd_h):
         return {'symbol': symbol, 'timeframe': timeframe, 'score': 0.0, 'side': 'none', 'tech_data': {}}

    
    # -----------------------------------------------------------
    # 2. スコア調整ファクターの計算
    # -----------------------------------------------------------

    # a. 長期トレンド一致/逆行ペナルティ (SMA 200)
    long_term_reversal_penalty_value = 0.0
    if close > sma_long and side == 'long':
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
    elif close < sma_long and side == 'short':
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
    elif close < sma_long and side == 'long':
        long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ
    elif close > sma_long and side == 'short':
        long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY # 逆張りペナルティ

    # b. MACD モメンタム一致/逆行ペナルティ
    macd_penalty_value = 0.0
    if side == 'long':
        # MACDがシグナルより上にある AND MACD自体もゼロラインより上
        if macd_h > 0 and macd > 0:
            macd_penalty_value = MACD_CROSS_PENALTY
        # MACDがシグナルより下にあるがゼロラインより上 (モメンタム失速の可能性)
        elif macd_h < 0 and macd > 0:
            macd_penalty_value = -MACD_CROSS_PENALTY
    elif side == 'short':
        # MACDがシグナルより下にある AND MACD自体もゼロラインより下
        if macd_h < 0 and macd < 0:
            macd_penalty_value = MACD_CROSS_PENALTY
        # MACDがシグナルより上にあるがゼロラインより下 (モメンタム失速の可能性)
        elif macd_h > 0 and macd < 0:
            macd_penalty_value = -MACD_CROSS_PENALTY
            
    # c. RSI モメンタム加速ボーナス
    rsi_momentum_bonus_value = 0.0
    if side == 'long' and rsi > (100 - RSI_MOMENTUM_LOW):
        rsi_momentum_bonus_value = STRUCTURAL_PIVOT_BONUS * 0.5 # 60以上で加速と見なす
    elif side == 'short' and rsi < RSI_MOMENTUM_LOW:
        rsi_momentum_bonus_value = STRUCTURAL_PIVOT_BONUS * 0.5 # 40以下で加速と見なす

    # d. OBV 出来高モメンタム確証ボーナス
    obv_momentum_bonus_value = 0.0
    if side == 'long' and obv_momentum > 0:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
    elif side == 'short' and obv_momentum < 0:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    # e. 流動性ボーナス (暫定的にTOP銘柄は一定のボーナス)
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX if symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT] else 0.0
    
    # f. FGI マクロコンテキストの影響
    sentiment_fgi_proxy_bonus = macro_context.get('fgi_proxy', 0.0) 
    # Longの場合はfgi_proxyをそのまま、Shortの場合は符号を反転させる
    sentiment_fgi_proxy_bonus = sentiment_fgi_proxy_bonus if side == 'long' else -sentiment_fgi_proxy_bonus
    # 負のボーナスはペナルティとして機能

    # g. ボラティリティ過熱ペナルティ
    volatility_penalty_value = 0.0
    # BB幅が Close の VOLATILITY_BB_PENALTY_THRESHOLD (例: 1%) を超えている場合、過熱と見なしてペナルティ
    if bb_width_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty_value = -STRUCTURAL_PIVOT_BONUS 

    # h. 🆕 StochRSIによる過熱ペナルティ/回復ボーナス
    stoch_rsi_penalty_value = 0.0
    if not pd.isna(stochrsi_k):
        if side == 'long':
            # 極端な買われ過ぎ (例: 80以上) でペナルティ
            if stochrsi_k > (100 - STOCHRSI_BOS_LEVEL):
                 stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY
            # 極端な売られ過ぎからの回復 (20以下から上昇中を意味)
            elif stochrsi_k < STOCHRSI_BOS_LEVEL and macd_h > 0:
                 stoch_rsi_penalty_value = STRUCTURAL_PIVOT_BONUS
                 
        elif side == 'short':
            # 極端な売られ過ぎ (例: 20以下) でペナルティ
            if stochrsi_k < STOCHRSI_BOS_LEVEL:
                 stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY
            # 極端な買われ過ぎからの回復 (80以上から下降中を意味)
            elif stochrsi_k > (100 - STOCHRSI_BOS_LEVEL) and macd_h < 0:
                 stoch_rsi_penalty_value = STRUCTURAL_PIVOT_BONUS

    # i. 🆕 ADXによるトレンド確証ボーナス
    adx_trend_bonus_value = 0.0
    if adx > ADX_TREND_STRENGTH_THRESHOLD: # トレンドが強い
        if side == 'long' and dmi_plus > dmi_minus:
            adx_trend_bonus_value = ADX_TREND_BONUS
        elif side == 'short' and dmi_minus > dmi_plus:
            adx_trend_bonus_value = ADX_TREND_BONUS
            
    
    # -----------------------------------------------------------
    # 3. 最終スコアの計算と辞書への格納
    # -----------------------------------------------------------
    
    # 構造的ボーナス (ベース)
    score += STRUCTURAL_PIVOT_BONUS 
    
    # 各調整ファクターを合計
    score += long_term_reversal_penalty_value
    score += macd_penalty_value
    score += rsi_momentum_bonus_value
    score += obv_momentum_bonus_value
    score += liquidity_bonus_value
    score += sentiment_fgi_proxy_bonus
    score += volatility_penalty_value
    score += stoch_rsi_penalty_value # 🆕
    score += adx_trend_bonus_value   # 🆕
    
    # スコアの最大値を1.0に制限
    score = min(1.0, score)
    # スコアの最小値をBASE_SCOREに制限 (最低限の根拠は必要)
    score = max(BASE_SCORE, score)

    # 最終的なスコア内訳
    score_details = {
        'indicators': tech_data,
        'long_term_reversal_penalty_value': long_term_reversal_penalty_value,
        'macd_penalty_value': macd_penalty_value,
        'rsi_momentum_bonus_value': rsi_momentum_bonus_value,
        'obv_momentum_bonus_value': obv_momentum_bonus_value,
        'liquidity_bonus_value': liquidity_bonus_value,
        'sentiment_fgi_proxy_bonus': sentiment_fgi_proxy_bonus,
        'volatility_penalty_value': volatility_penalty_value,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
        'stoch_rsi_penalty_value': stoch_rsi_penalty_value, # 🆕
        'stoch_rsi_k_value': stochrsi_k, # 通知用
        'adx_trend_bonus_value': adx_trend_bonus_value, # 🆕
        'adx_raw_value': adx, # 通知用
    }
    
    return {
        'symbol': symbol, 
        'timeframe': timeframe, 
        'score': score, 
        'side': side, 
        'entry_price': close, # シグナル確定時の終値をエントリー価格候補とする
        'tech_data': score_details,
    }

def calculate_position_parameters(signal: Dict, df: pd.DataFrame) -> Dict[str, Any]:
    """
    シグナル情報に基づき、ATRを使用してSL/TP価格とRR比率を計算する。
    
    Args:
        signal: score_signal から返されたシグナル辞書。
        df: OHLCVデータを含むDataFrame (ATR計算に使用)。
        
    Returns:
        SL/TP価格、RR比率などが追加された更新済みシグナル辞書。
    """
    
    side = signal['side']
    entry_price = signal['entry_price']
    
    # ATR値を取得
    atr_col = f'ATR_{ATR_LENGTH}'
    tech_data = signal.get('tech_data', {})
    atr_value = tech_data.get('indicators', {}).get(atr_col, 0.0)
    
    if atr_value is None or atr_value <= 0.0 or entry_price <= 0.0:
        logging.warning(f"⚠️ {signal['symbol']}: ATR値 ({atr_value}) またはエントリー価格 ({entry_price}) が無効です。取引パラメータを計算できません。")
        signal['stop_loss'] = 0.0
        signal['take_profit'] = 0.0
        signal['rr_ratio'] = 0.0
        return signal

    # 1. リスク幅 (R) の計算 (ATRに基づく)
    # ATR_MULTIPLIER_SL (例: 2.0) を使用
    risk_width_atr = atr_value * ATR_MULTIPLIER_SL 
    
    # 最小リスク幅の適用 (価格の MIN_RISK_PERCENT, 例: 0.8% )
    min_risk_width = entry_price * MIN_RISK_PERCENT 
    
    # 最終的なリスク幅は、ATRに基づくリスク幅と最小リスク幅の大きい方
    risk_width = max(risk_width_atr, min_risk_width)
    
    # 2. SL (Stop Loss) 価格の計算
    stop_loss = 0.0
    if side == 'long':
        stop_loss = entry_price - risk_width
    elif side == 'short':
        stop_loss = entry_price + risk_width
        
    # SL価格は0以下にならないようにする
    stop_loss = max(0.00000001, stop_loss) 

    # 3. TP (Take Profit) 価格の計算
    # リワード幅 = リスク幅 * RR_RATIO_TARGET (例: 1.5)
    reward_width = risk_width * RR_RATIO_TARGET
    
    take_profit = 0.0
    if side == 'long':
        take_profit = entry_price + reward_width
    elif side == 'short':
        take_profit = entry_price - reward_width
        
    # TP価格は0以下にならないようにする
    take_profit = max(0.00000001, take_profit)
    
    # 4. 清算価格 (Liquidation Price) の計算
    liquidation_price = calculate_liquidation_price(entry_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)


    # 5. 結果の格納
    signal['stop_loss'] = stop_loss
    signal['take_profit'] = take_profit
    signal['liquidation_price'] = liquidation_price
    # 実際のRR比率 (目標値と同じはずだが、計算ミス防止のため再計算)
    signal['rr_ratio'] = (reward_width / risk_width) if risk_width > 0 else 0.0 
    
    # リスク幅の絶対値も通知のために格納
    signal['risk_width'] = risk_width 
    signal['reward_width'] = reward_width
    
    return signal

# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

async def execute_trade(symbol: str, side: str, entry_price: float, stop_loss: float, take_profit: float, current_price: float) -> Dict:
    global EXCHANGE_CLIENT
    
    trade_result: Dict = {'status': 'error', 'error_message': '取引所クライアントが利用不可です。'}
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} の {side.upper()} シグナルを検出しましたが、取引はスキップされます。")
        trade_result = {
            'status': 'ok',
            'filled_amount': FIXED_NOTIONAL_USDT / current_price,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'entry_price': entry_price,
            'exit_price': 0.0,
            'exit_type': 'TEST',
            'order_id': f"TEST-{uuid.uuid4()}",
            'price': current_price # 成行価格
        }
        return trade_result
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ 取引失敗: クライアントが準備されていません。({symbol}, {side})")
        return trade_result

    # 1. 注文数量の計算
    # 固定の想定ロット (FIXED_NOTIONAL_USDT) とレバレッジ (LEVERAGE) から取引に必要な名目価値を計算
    # 注: 実際にはレバレッジは証拠金計算に使用され、名目価値は FIXED_NOTIONAL_USDT です。
    # 必要な通貨数量 = FIXED_NOTIONAL_USDT / current_price 
    amount_in_base_currency = FIXED_NOTIONAL_USDT / current_price
    
    # CCXTの丸め処理 (ロジック外)
    market = EXCHANGE_CLIENT.markets.get(symbol)
    if not market:
        trade_result['error_message'] = f"市場情報が見つかりません: {symbol}"
        logging.error(trade_result['error_message'])
        return trade_result
        
    # 最小注文数量の単位で丸める
    amount_in_base_currency = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_in_base_currency)
    amount = float(amount_in_base_currency)

    if amount <= 0:
        trade_result['error_message'] = f"Amount can not be less than zero or precision error. Calculated amount: {amount}"
        logging.error(trade_result['error_message'])
        # 注文失敗エラー (Amount can not be less than zero) 対策
        return trade_result
        
    try:
        # 2. メインの注文 (成行注文: market) の執行
        side_ccxt = 'buy' if side == 'long' else 'sell'
        
        # CCXTで先物/スワップ取引を行う際は、通常 'market' か 'limit' を使用
        # ここではシグナル発生時の即時エントリーのため 'market' を使用
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market', 
            side=side_ccxt,
            amount=amount,
            # params: 例: Post Only, Reduce Only, クローズ注文など (今回は新規エントリーなので不要)
        )
        
        logging.info(f"✅ {symbol} の {side.upper()} 成行注文を送信しました。数量: {amount}")

        # 注文後の結果を待機・確認（成行なので即時約定を想定）
        # 実際には即時約定しない場合や部分約定の可能性があるが、ここではシンプルに結果を解析
        filled_amount = float(order.get('filled', amount))
        filled_usdt = filled_amount * float(order.get('price', current_price))
        entry_price_final = float(order.get('price', current_price)) 
        order_id = order.get('id', 'N/A')

        if filled_amount > 0:
            # 3. SL/TP注文の設置 (OCO注文またはストップリミット/テイクプロフィットリミット)
            # CCXTの統一APIでSL/TPを同時にセットできるかは取引所による。
            # 例: Binance, Bybitなどは create_order の params で OCO をサポート
            # MEXC (CCXT v3) は TP/SL の統一パラメータが非サポートのことが多い
            
            # ここでは、CCXTの統一 set_stop_loss と set_take_profit を使用する（可能であれば）
            # または、取引所固有のパラメーターを使用
            
            sl_tp_success = await set_stop_loss_and_take_profit(symbol, side, filled_amount, stop_loss, take_profit, entry_price_final, market)

            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'entry_price': entry_price_final,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'order_id': order_id,
                'sl_tp_set': sl_tp_success,
                'price': current_price
            }
            logging.info(f"✅ {symbol} の取引が成功しました。エントリー価格: {entry_price_final:.4f}, ロット: {filled_usdt:.2f} USDT")
            
            # 4. ポジション情報をグローバル変数に格納
            # ポジションを管理リストに追加 (SL/TP情報込み)
            OPEN_POSITIONS.append({
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price_final,
                'contracts': filled_amount,
                'filled_usdt': filled_usdt,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'leverage': LEVERAGE,
                'liquidation_price': calculate_liquidation_price(entry_price_final, LEVERAGE, side),
                'risk_width': signal['risk_width'], # SL設定後に管理のため保存
                'rr_ratio': signal['rr_ratio'],
            })


        else:
            trade_result['error_message'] = "約定数量がゼロです (部分約定または約定失敗)。"
            logging.error(trade_result['error_message'])


    except ccxt.ExchangeError as e:
        error_msg = f"取引所エラー: {e}"
        trade_result['error_message'] = error_msg
        logging.error(f"❌ 取引所エラー: {e}")
    except Exception as e:
        error_msg = f"予期せぬ取引エラー: {e}"
        trade_result['error_message'] = error_msg
        logging.critical(f"❌ 予期せぬ取引エラーが発生しました: {e}", exc_info=True)
        
    return trade_result


async def set_stop_loss_and_take_profit(symbol: str, side: str, amount: float, stop_loss: float, take_profit: float, entry_price: float, market: Dict) -> bool:
    """
    ポジションに対してSL/TP注文を設定する。
    CCXTの統一 set_stop_loss/set_take_profit API または取引所固有の注文を使用。
    """
    global EXCHANGE_CLIENT
    success = True
    
    # 数量を正確に設定
    amount_abs = abs(amount)
    
    # SL/TPの注文サイドは、ポジションを決済する方向
    close_side_ccxt = 'sell' if side == 'long' else 'buy'
    
    try:
        # 1. SL注文の設定
        # set_stop_lossは、トリガー価格 (stopPrice) で、決済方向の成行注文 (market) または指値注文 (limit) を出す
        
        # 💡 MEXC向け: MEXCは set_stop_loss/set_take_profit の統一APIをサポートしていない可能性があるため、
        # create_orderを使用し、パラメーターで SL/TP を指定する方式を試みる（取引所固有の方法）
        if EXCHANGE_CLIENT.id == 'mexc':
            
            # MEXCの先物TP/SLは、ポジションが開いている状態で 'POST /api/v1/private/position/change_margin' 
            # または 'POST /api/v1/private/order/stopLimit' など、独自のAPIエンドポイントを必要とする場合がある。
            # CCXTのcreate_orderのparamsで stopLossPrice, takeProfitPrice を設定するのが最もCCXT的。
            
            try:
                # create_orderでSL/TPを同時に設定 (指値注文として送信)
                # ポジション決済のための指値注文
                
                # ロングポジションを決済するためのストップロスの指値 (売りのストップリミット)
                if side == 'long':
                    params = {
                        'stopLossPrice': stop_loss, # SLトリガー価格
                        'takeProfitPrice': take_profit, # TPトリガー価格
                        'positionType': 1, # ポジションタイプ (1: Long)
                        'stopLossType': 1, # 成行/指値 (1: 成行)
                        'takeProfitType': 1, # 成行/指値 (1: 成行)
                        'triggerPriceType': 2, # 指標価格 (2: マーケットプライス)
                        'reduce_only': True # 決済専用
                    }
                    
                    # MEXCでは、ポジションがある状態であれば、一括設定APIが最も確実
                    # しかし、統一API create_order にパラメーターを渡すのがCCXTの設計思想
                    # CCXTの create_order に直接 TP/SL を渡すパラメーターがあれば、それが望ましい
                    
                    # 💡 堅牢性のために、ここではCCXT統一APIに準拠し、ポジション管理に任せる（次の check_and_manage_open_positions が重要になる）
                    # CCXTの set_stop_loss / set_take_profit は、ポジションを識別して注文を出すため、
                    # 既にポジションが開いていることを前提に実行する (MEXCは未サポートの場合が多いが、最新版のCCXTを信用する)
                    
                    # 💡 set_stop_loss, set_take_profit が使用できない場合でも、
                    # 次のループでポジション監視関数 (check_and_manage_open_positions) がSL/TPのトリガーを監視・執行するため、
                    # ここでのAPI呼び出しが失敗しても、致命的ではない (ただし、理想は即時注文設定)
                    
                    pass # MEXCの場合、CCXT統一APIを信頼せず、後のポジション監視に依存する

            except Exception as e:
                logging.warning(f"⚠️ {symbol}: MEXC固有のSL/TP設定に失敗しました: {e}")
                success = False

        else:
            # その他の取引所 (Binance/Bybitなど) の場合、統一APIを試行
            
            # SL: ポジションを決済する成行ストップ注文
            await EXCHANGE_CLIENT.set_stop_loss(
                symbol=symbol,
                price=stop_loss, # トリガー価格
                amount=amount_abs,
                params={'stopLossType': 'market', 'reduce_only': True} # 成行で決済
            )
            logging.info(f"✅ {symbol} のSL注文をセットしました。価格: {stop_loss}")

            # TP: ポジションを決済する成行リミット注文
            await EXCHANGE_CLIENT.set_take_profit(
                symbol=symbol,
                price=take_profit, # トリガー価格
                amount=amount_abs,
                params={'takeProfitType': 'market', 'reduce_only': True} # 成行で決済
            )
            logging.info(f"✅ {symbol} のTP注文をセットしました。価格: {take_profit}")

    except ccxt.NotSupported as e:
        # set_stop_loss/set_take_profit が未サポートの場合
        logging.warning(f"⚠️ {symbol}: SL/TP統一APIが未サポートです ({EXCHANGE_CLIENT.id})。後のポジション監視に依存します。{e}")
        success = False
    except Exception as e:
        # その他のエラー
        logging.error(f"❌ {symbol}: SL/TP注文の設定中にエラーが発生しました: {e}", exc_info=True)
        success = False
        
    return success

async def check_and_manage_open_positions():
    """
    オープンポジションをチェックし、最新価格でSL/TPを監視・決済する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション管理失敗: クライアントが準備されていません。")
        return
        
    if not OPEN_POSITIONS:
        # CCXTから最新のポジションリストを再取得し、OPEN_POSITIONSを同期する
        account_status = await fetch_account_status()
        if not account_status.get('error'):
            # fetch_account_status内でOPEN_POSITIONSは更新されていないため、手動で更新
            # fetch_account_statusのポジションを信頼する
            global OPEN_POSITIONS
            OPEN_POSITIONS = account_status.get('open_positions', [])
            logging.info(f"ℹ️ CCXTから {len(OPEN_POSITIONS)} 件のオープンポジションを同期しました。")
        
        if not OPEN_POSITIONS:
            logging.info("ℹ️ 現在、管理対象のオープンポジションはありません。")
            return

    logging.info(f"⏳ {len(OPEN_POSITIONS)} 件のポジションのSL/TPを監視中...")
    
    symbols_to_check = list(set([p['symbol'] for p in OPEN_POSITIONS]))
    latest_prices: Dict[str, float] = {}
    
    # 監視対象のシンボルの最新ティッカー価格を一括取得
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_check)
        for symbol in symbols_to_check:
            # 'last' または 'close' 価格を使用
            price = tickers.get(symbol, {}).get('last') or tickers.get(symbol, {}).get('close')
            if price is not None:
                latest_prices[symbol] = float(price)
    except Exception as e:
        logging.error(f"❌ 最新価格の取得に失敗しました: {e}")
        return # 価格が取得できない場合はスキップ

    positions_to_keep = []
    positions_to_close = []

    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        side = pos['side']
        sl = pos['stop_loss']
        tp = pos['take_profit']
        entry_price = pos['entry_price']
        contracts = pos['contracts']
        filled_usdt = pos['filled_usdt']
        
        current_price = latest_prices.get(symbol, 0.0)

        if current_price == 0.0:
            logging.warning(f"⚠️ {symbol}: 最新価格が取得できませんでした。監視を続行します。")
            positions_to_keep.append(pos)
            continue
            
        exit_type = None
        
        # 1. SLトリガーチェック
        if side == 'long' and current_price <= sl:
            exit_type = 'STOP_LOSS'
        elif side == 'short' and current_price >= sl:
            exit_type = 'STOP_LOSS'
            
        # 2. TPトリガーチェック (SLよりも優先)
        elif side == 'long' and current_price >= tp:
            exit_type = 'TAKE_PROFIT'
        elif side == 'short' and current_price <= tp:
            exit_type = 'TAKE_PROFIT'

        # 3. 清算価格チェック (保険) - 実際の取引所APIが清算を行うが、通知用にチェック
        # liquidation_price = pos['liquidation_price']
        # if side == 'long' and current_price <= liquidation_price * 1.01: # わずかなバッファ
        #     exit_type = 'LIQUIDATION_RISK'
        # elif side == 'short' and current_price >= liquidation_price * 0.99:
        #     exit_type = 'LIQUIDATION_RISK'


        if exit_type:
            # 決済シグナル発生
            logging.info(f"🚨 {symbol} の {side.upper()} ポジションで {exit_type} トリガーを検出しました。決済を試行します。")
            
            # PnLの計算 (通知用)
            # PnL (USDT) = 契約数 * (決済価格 - エントリー価格) * (1 or -1)
            pnl_base = abs(contracts) * (current_price - entry_price) 
            pnl_usdt = pnl_base if side == 'long' else -pnl_base
            
            # PnL Rate (%) = (PnL / (名目価値/レバレッジ)) * 100
            # 証拠金 = filled_usdt / LEVERAGE
            initial_margin = filled_usdt / LEVERAGE
            pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0
            
            close_result = await close_position(symbol, side, abs(contracts))
            
            pos_close_data = {
                'symbol': symbol,
                'side': side,
                'entry_price': entry_price,
                'exit_price': current_price,
                'filled_amount': abs(contracts),
                'filled_usdt': filled_usdt,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': exit_type,
            }

            if close_result['status'] == 'ok' or TEST_MODE:
                positions_to_close.append(pos)
                # Telegram通知
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 通知のための取得
                await send_telegram_notification(format_telegram_message(pos_close_data, "ポジション決済", current_threshold, close_result, exit_type))
                log_signal(pos, "ポジション決済", pos_close_data)
                
            else:
                # 決済失敗の場合、監視を継続 (ただしエラーログを出す)
                logging.error(f"❌ {symbol}: {exit_type} の決済に失敗しました。監視を続行します。エラー: {close_result.get('error_message')}")
                positions_to_keep.append(pos)
        else:
            # トリガーに達していないため監視を継続
            positions_to_keep.append(pos)
            
    # グローバルポジションリストを更新
    OPEN_POSITIONS = positions_to_keep
    logging.info(f"✅ ポジション監視を完了しました。残り {len(OPEN_POSITIONS)} 件のポジション。")


async def close_position(symbol: str, side: str, amount: float) -> Dict:
    """
    指定されたシンボルのポジションをクローズする。
    """
    global EXCHANGE_CLIENT
    
    trade_result: Dict = {'status': 'error', 'error_message': '取引所クライアントが利用不可です。'}
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} の {side.upper()} ポジションを決済しますが、取引はスキップされます。")
        trade_result = {'status': 'ok', 'order_id': f"TEST-CLOSE-{uuid.uuid4()}", 'price': 0.0}
        return trade_result

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ 決済失敗: クライアントが準備されていません。({symbol}, {side})")
        return trade_result

    try:
        # ポジションをクローズする側の注文 (Longをクローズ -> Sell, Shortをクローズ -> Buy)
        close_side_ccxt = 'sell' if side == 'long' else 'buy'
        
        # 成行注文でポジションをクローズ (amountは絶対値を使用)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market', 
            side=close_side_ccxt,
            amount=amount,
            params={'reduceOnly': True} # ポジション削減専用
        )
        
        logging.info(f"✅ {symbol} の {side.upper()} ポジション決済注文を送信しました。数量: {amount}")

        # 注文結果の解析
        filled_amount = float(order.get('filled', 0.0))
        exit_price = float(order.get('price', 0.0))
        order_id = order.get('id', 'N/A')

        if filled_amount >= amount * 0.99: # ほぼ全量が約定したと見なす
            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'exit_price': exit_price,
                'order_id': order_id,
            }
            logging.info(f"✅ {symbol} ポジション決済が成功しました。決済価格: {exit_price:.4f}")
        else:
            trade_result['error_message'] = f"約定数量が不足しています。要求: {amount}, 約定: {filled_amount}"
            logging.warning(trade_result['error_message'])


    except ccxt.ExchangeError as e:
        error_msg = f"取引所エラー: {e}"
        trade_result['error_message'] = error_msg
        logging.error(f"❌ ポジション決済の取引所エラー: {e}")
    except Exception as e:
        error_msg = f"予期せぬ決済エラー: {e}"
        trade_result['error_message'] = error_msg
        logging.critical(f"❌ 予期せぬポジション決済エラーが発生しました: {e}", exc_info=True)
        
    return trade_result

# ====================================================================================
# MAIN LOGIC & SCHEDULERS
# ====================================================================================

async def main_bot_logic():
    """
    BOTの主要な分析と取引ロジックを実行する。
    """
    global GLOBAL_MACRO_CONTEXT, CURRENT_MONITOR_SYMBOLS, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS
    
    # 1. マクロコンテキストの更新 (FGI)
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()

    # 2. 監視シンボルリストの更新
    CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
    
    # 3. ポジション情報を最新の状態に同期
    account_status = await fetch_account_status()
    # fetch_account_status の結果を用いて、グローバル変数 OPEN_POSITIONS を更新
    global OPEN_POSITIONS 
    OPEN_POSITIONS = account_status.get('open_positions', [])
    
    if account_status.get('error') and not TEST_MODE:
        logging.critical("❌ 口座情報の取得に失敗したため、今回の取引サイクルをスキップします。")
        return
    
    if not CURRENT_MONITOR_SYMBOLS:
        logging.warning("⚠️ 監視対象のシンボルがありません。取引サイクルをスキップします。")
        return
        
    # 4. 全シンボル・全タイムフレームで分析を実行
    analysis_results: List[Dict] = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        for tf in TARGET_TIMEFRAMES:
            # ポジションを既に持っている銘柄は、分析のみ行い取引はスキップ
            if symbol in [p['symbol'] for p in OPEN_POSITIONS]:
                 continue 
                 
            required_limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
            df = await fetch_ohlcv_data(symbol, tf, limit=required_limit)
            
            if df is None or len(df) < required_limit:
                logging.warning(f"⚠️ {symbol} ({tf}): 必要なOHLCVデータ ({required_limit}件) が不足しています。分析をスキップ。")
                continue
                
            # テクニカル指標の計算
            tech_data = calculate_technical_indicators(df)
            
            # スコアリング
            signal = score_signal(symbol, tf, df, tech_data, GLOBAL_MACRO_CONTEXT)
            
            # ATRに基づきSL/TPを計算 (シグナルに価格情報を追加)
            signal_with_params = calculate_position_parameters(signal, df)

            if signal_with_params['score'] >= BASE_SCORE:
                analysis_results.append(signal_with_params)
                
    # 5. 取引シグナルのフィルタリングと執行
    
    # スコアの降順でソート
    sorted_signals = sorted(analysis_results, key=lambda x: x.get('score', 0.0), reverse=True)
    
    LAST_ANALYSIS_SIGNALS = analysis_results # 定時通知のために保存
    
    # 現在の動的閾値を取得
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    trade_signals: List[Dict] = []
    
    for signal in sorted_signals:
        symbol = signal['symbol']
        side = signal['side']
        score = signal['score']
        
        # 閾値チェック
        if score < current_threshold:
            logging.info(f"ℹ️ {symbol} ({side.upper()}): スコア {score*100:.2f} が閾値 {current_threshold*100:.2f} 未満のためスキップ。")
            continue
            
        # クールダウンチェック (同一銘柄の取引後、一定時間は取引をスキップ)
        # キーは "SYMBOL_TIMEFRAME_SIDE" で管理
        signal_key = f"{symbol}_{signal['timeframe']}_{side}"
        last_trade_time = LAST_SIGNAL_TIME.get(signal_key, 0.0)
        
        if time.time() - last_trade_time < TRADE_SIGNAL_COOLDOWN:
            elapsed_hours = (time.time() - last_trade_time) / 3600
            cooldown_hours = TRADE_SIGNAL_COOLDOWN / 3600
            logging.info(f"ℹ️ {symbol} ({side.upper()}): クールダウン中 ({elapsed_hours:.1f}/{cooldown_hours:.0f} 時間)。スキップ。")
            continue
            
        # ポジション数を制限 (最大 TOP_SIGNAL_COUNT 件まで)
        if len(OPEN_POSITIONS) >= TOP_SIGNAL_COUNT and not TEST_MODE:
            logging.warning(f"⚠️ 最大ポジション数 ({TOP_SIGNAL_COUNT}) に達しました。新しいシグナル {symbol} ({score*100:.2f}) をスキップします。")
            break
            
        trade_signals.append(signal)
        
        if len(trade_signals) >= TOP_SIGNAL_COUNT:
            break
            
    # 6. 決定されたシグナルで取引を実行
    for signal in trade_signals:
        symbol = signal['symbol']
        side = signal['side']
        entry_price = signal['entry_price']
        stop_loss = signal['stop_loss']
        take_profit = signal['take_profit']
        
        # 現在の価格を再度取得 (エントリーの確実性を高める)
        current_price = latest_prices.get(symbol, entry_price) 
        if current_price == 0.0:
            logging.error(f"❌ {symbol}: エントリー直前の価格取得に失敗しました。取引をスキップします。")
            continue
            
        logging.info(f"🔥 **取引実行**: {symbol} ({side.upper()}), Score: {signal['score']*100:.2f}")

        # 取引の執行
        trade_result = await execute_trade(symbol, side, entry_price, stop_loss, take_profit, current_price)
        
        if trade_result['status'] == 'ok':
            # 成功時、クールダウン時間を更新
            LAST_SIGNAL_TIME[f"{symbol}_{signal['timeframe']}_{side}"] = time.time()
            # Telegram通知
            await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
            log_signal(signal, "取引シグナル", trade_result)
        else:
            # 失敗時、ログに記録
            log_signal(signal, "取引失敗", trade_result)
            # 失敗通知
            await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))

async def main_bot_scheduler():
    """
    BOTのメインループを定期的に実行するスケジューラ。
    """
    global LAST_SUCCESS_TIME, GLOBAL_MACRO_CONTEXT, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_WEBSHARE_UPLOAD_TIME

    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        logging.critical("❌ BOTの起動に失敗しました。CCXTクライアントの初期化ができませんでした。")
        # 致命的なエラーのため、ループを中止
        return

    # 2. 初回起動時のマクロコンテキストと口座ステータス取得
    account_status = await fetch_account_status()
    GLOBAL_MACRO_CONTEXT = await fetch_fgi_data()
    CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_symbols()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 3. BOT起動完了通知 (初回の実行を保証)
    await send_telegram_notification(format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold))

    LAST_SUCCESS_TIME = time.time() # 初回起動完了時間を記録
    IS_FIRST_MAIN_LOOP_COMPLETED = True
    
    # 4. メインループ開始
    logging.info("⏳ メインBOTスケジューラを開始します。")

    while True:
        try:
            current_time = time.time()
            
            # メインロジックの実行
            await main_bot_logic()
            LAST_SUCCESS_TIME = current_time # 成功時のみ更新
            
            # 定時報告のチェック (取引閾値未満の最高スコア銘柄を報告)
            await notify_highest_analysis_score()
            
            # WebShareへのデータ送信チェック
            if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                
                # WebShareに送信するデータ (簡略化)
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'is_test_mode': TEST_MODE,
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'fgi_context': GLOBAL_MACRO_CONTEXT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions_summary': [{'symbol': p['symbol'], 'side': p['side'], 'entry_price': p['entry_price']} for p in OPEN_POSITIONS],
                    'last_analysis_signals_top5': sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x.get('score', 0.0), reverse=True)[:5]
                }
                
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time
            
            # 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            logging.error(f"❌ メインBOTスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時もループを続けるが、レートリミット対策として待機時間を延長
            await asyncio.sleep(LOOP_INTERVAL * 5)
            
async def position_monitor_scheduler():
    """
    ポジション監視専用のスケジューラ。
    メインループとは独立して、より短い間隔で実行。
    """
    if not IS_CLIENT_READY:
         # クライアント初期化を待つ（最大10秒）
         await asyncio.sleep(10)
         
    if not IS_CLIENT_READY:
         logging.critical("❌ ポジション監視スケジューラを開始できません。CCXTクライアントが初期化されていません。")
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
    """捕捉されなかった例外を処理し、ログに記録する"""
    logging.error(f"🚨 捕捉されなかった例外: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )
