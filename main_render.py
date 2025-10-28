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
                    logging.warning(f"⚠️ MEXCのフォールバックロジックでUSDT総資産を更新: {format_usdt(total_usdt_balance)}")


        # 総資産をグローバル変数に保存 (リスク管理に使用)
        ACCOUNT_EQUITY_USDT = float(total_usdt_balance)
        logging.info(f"💰 口座総資産 (USDT Equity): {format_usdt(ACCOUNT_EQUITY_USDT)}")
        
        # オープンポジション情報の取得
        # MEXCはfetch_positionsを使用。Binance/Bybitはfetch_positions_riskを使用
        positions: List[Dict] = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions = await EXCHANGE_CLIENT.fetch_positions()
            
        open_positions = []
        for p in positions:
            # ポジションサイズが0より大きい、かつUSDT建て先物シンボルのみを抽出
            if (p.get('contracts', 0) != 0 or p.get('info', {}).get('vol', 0) != 0) and \
               p.get('symbol', '').endswith('/USDT') and \
               p.get('type') in ['swap', 'future']:
                
                # 必要な情報を抽出/整形
                entry_price_raw = p.get('entryPrice', 0.0)
                contracts_raw = p.get('contracts', 0.0)
                
                # contractsが取れない場合、MEXC特有の 'vol' フィールドを試す
                if contracts_raw == 0 and EXCHANGE_CLIENT.id == 'mexc':
                    contracts_raw = float(p.get('info', {}).get('vol', 0.0))
                
                # 名目価値 (Notional Value) を計算
                notional_value = abs(contracts_raw * entry_price_raw)
                
                # side (long/short) の判定
                side = 'long' if contracts_raw > 0 else 'short'
                
                # 最終的なポジション情報 (ボットが管理すべき最低限の情報)
                open_positions.append({
                    'symbol': p['symbol'],
                    'side': side,
                    'contracts': contracts_raw,
                    'entry_price': entry_price_raw,
                    'filled_usdt': notional_value, # 名目価値
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'timestamp': p.get('timestamp', int(time.time() * 1000)),
                    'stop_loss': 0.0, # ボット内で動的に設定されるため、ここでは初期値
                    'take_profit': 0.0, # ボット内で動的に設定されるため、ここでは初期値
                    'initial_risk_usdt': 0.0 # ボット内で動的に設定されるため、ここでは初期値
                })

        # グローバル変数を更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"✅ オープンポジション ({len(OPEN_POSITIONS)}件) の情報を取得しました。")

        return {
            'total_usdt_balance': float(total_usdt_balance),
            'open_positions': OPEN_POSITIONS,
            'error': False
        }

    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ 口座ステータス取得失敗 - 取引所接続エラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中の予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}


async def fetch_historical_ohlcv(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """
    指定されたシンボルの過去のOHLCVデータをCCXTから取得し、Pandas DataFrameとして返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ OHLCVデータ取得失敗: CCXTクライアントが準備できていません。")
        return None
        
    try:
        # ccxtのシンボル形式 (例: BTC/USDT) に変換
        ccxt_symbol = symbol.replace('_', '/') 
        
        # ccxt.fetch_ohlcv はミリ秒単位のタイムスタンプを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            ccxt_symbol, 
            timeframe, 
            limit=limit
        )
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} - {timeframe}: OHLCVデータが空でした。")
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # タイムスタンプを日付時刻に変換し、インデックスに設定
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize('UTC').dt.tz_convert(JST)
        df.set_index('datetime', inplace=True)
        
        # ログメッセージ
        logging.info(f"✅ {symbol} - {timeframe}: OHLCVデータ ({len(df)}本) を取得しました。最新: {df.index[-1].strftime('%Y-%m-%d %H:%M')}")
        
        return df
        
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ {symbol} - {timeframe}: DDoS保護エラー (レート制限): {e}")
    except ccxt.ExchangeError as e:
        logging.error(f"❌ {symbol} - {timeframe}: 取引所エラー (シンボル無効/データ不足): {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCVデータ取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return None

def get_trend_strength_score(series: pd.Series, periods: int = 5) -> float:
    """
    直近のローソク足(periods)の傾き（モメンタム）を計算し、スコアを返す。
    
    :param series: 終値またはMACDヒストグラムのシリーズ
    :param periods: 傾きを計算するローソク足の数 (例: 5本)
    :return: 傾きに基づくモメンタムスコア (1.0, -1.0, 0.0)
    """
    if len(series) < periods or series.isnull().any():
        return 0.0
        
    try:
        # 直近のデータのみを使用
        recent_data = series.iloc[-periods:]
        
        # 0, 1, 2, 3, 4 のインデックスを作成
        x = np.arange(periods)
        
        # 線形回帰 (np.polyfit) を使用して傾きを計算
        p = np.polyfit(x, recent_data, deg=1)
        slope = p[0]
        
        if slope > 0:
            return 1.0
        elif slope < 0:
            return -1.0
        else:
            return 0.0

    except ValueError as e:
        logging.warning(f"⚠️ 傾き計算エラー (ValueError): {e}")
        return 0.0
    except Exception as e:
        logging.error(f"❌ 傾き計算中に予期せぬエラー: {e}", exc_info=True)
        return 0.0


async def calculate_indicators(symbol: str, timeframe: str, df: pd.DataFrame) -> Tuple[pd.DataFrame, Optional[Dict]]:
    """
    指定されたDataFrameを使用してテクニカル指標を計算し、DataFrameと辞書で返す。
    """
    if df is None or len(df) < LONG_TERM_SMA_LENGTH + ATR_LENGTH:
        logging.warning(f"⚠️ {symbol} - {timeframe}: データ不足。計算をスキップします。")
        return df, None
        
    indicators = {}
    
    # 1. SMA (Simple Moving Average)
    try:
        # DFにSMA_200を追加 (MACDの計算には不要だが、参照用)
        df['SMA_200'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
        indicators['SMA_200'] = df['SMA_200'].iloc[-1]
    except Exception as e:
        logging.warning(f"⚠️ SMAの計算エラー: {e}")
        indicators['SMA_200'] = np.nan

    # 2. MACD (Moving Average Convergence Divergence)
    try:
        macd_result = ta.macd(df['close'], fast=12, slow=26, signal=9, append=True) # DFにMACDを追加
        indicators['MACDh'] = df['MACDh_12_26_9'].iloc[-1]
        indicators['MACD'] = df['MACD_12_26_9'].iloc[-1]
        indicators['MACDs'] = df['MACDs_12_26_9'].iloc[-1]
        indicators['MACD_momentum_score'] = get_trend_strength_score(df['MACDh_12_26_9'])
        
    except Exception as e:
        logging.warning(f"⚠️ MACDの計算エラー: {e}")
        indicators['MACDh'] = np.nan
        indicators['MACD'] = np.nan
        indicators['MACDs'] = np.nan
        indicators['MACD_momentum_score'] = 0.0
    
    # 3. RSI (Relative Strength Index)
    try:
        df['RSI_14'] = ta.rsi(df['close'], length=14) # DFにRSI_14を追加
        indicators['RSI'] = df['RSI_14'].iloc[-1]
    except Exception as e:
        logging.warning(f"⚠️ RSIの計算エラー: {e}")
        indicators['RSI'] = np.nan
    
    # 4. Bollinger Bands (BBANDS)
    try:
        bbands = ta.bbands(df['close'], length=20, std=2)
        indicators['BB_upper'] = bbands.iloc[-1]['BBU_20_2.0']
        indicators['BB_lower'] = bbands.iloc[-1]['BBL_20_2.0']
        indicators['BB_width'] = bbands.iloc[-1]['BBW_20_2.0']
    except Exception as e:
        logging.warning(f"⚠️ BBANDSの計算エラー: {e}")
        indicators['BB_upper'] = np.nan
        indicators['BB_lower'] = np.nan
        indicators['BB_width'] = np.nan

    # 5. ATR (Average True Range)
    try:
        indicators['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH).iloc[-1]
    except Exception as e:
        logging.warning(f"⚠️ ATRの計算エラー: {e}")
        indicators['ATR'] = np.nan

    # 6. OBV (On Balance Volume)
    try:
        obv_series = ta.obv(df['close'], df['volume'])
        indicators['OBV'] = obv_series.iloc[-1]
        indicators['OBV_momentum_score'] = get_trend_strength_score(obv_series)
        
    except Exception as e:
        logging.warning(f"⚠️ OBVの計算エラー: {e}")
        indicators['OBV'] = np.nan
        indicators['OBV_momentum_score'] = 0.0

    # 7. 🆕 StochRSI (Stochastic RSI)
    try:
        stoch_rsi = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
        if stoch_rsi is not None and not stoch_rsi.empty and len(stoch_rsi) >= 1:
            indicators['stoch_rsi_k'] = stoch_rsi.iloc[-1][f'STOCHRSIk_14_14_3_3']
            indicators['stoch_rsi_d'] = stoch_rsi.iloc[-1][f'STOCHRSId_14_14_3_3']
        else:
            indicators['stoch_rsi_k'] = np.nan
            indicators['stoch_rsi_d'] = np.nan
    except Exception as e:
        logging.warning(f"⚠️ {symbol} - {timeframe} のStochRSI計算エラー: {e}")
        indicators['stoch_rsi_k'] = np.nan
        indicators['stoch_rsi_d'] = np.nan

    # 8. 🆕 ADX (Average Directional Index)
    try:
        adx_result = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_result is not None and not adx_result.empty and len(adx_result) >= 1:
            indicators['adx'] = adx_result.iloc[-1]['ADX_14']
            indicators['adx_plus'] = adx_result.iloc[-1]['DMP_14']
            indicators['adx_minus'] = adx_result.iloc[-1]['DMN_14']
        else:
            indicators['adx'] = np.nan
            indicators['adx_plus'] = np.nan
            indicators['adx_minus'] = np.nan
    except Exception as e:
        logging.warning(f"⚠️ {symbol} - {timeframe} のADX計算エラー: {e}")
        indicators['adx'] = np.nan
        indicators['adx_plus'] = np.nan
        indicators['adx_minus'] = np.nan
    
    return df, indicators


def calculate_trade_score(symbol: str, timeframe: str, data: Dict[str, Any], df: pd.DataFrame, macro_context: Dict, liquidity_rank: int) -> Optional[Dict]:
    """
    計算されたテクニカル指標と市場環境に基づき、取引の総合スコアを計算する。
    """
    indicators = data.get('indicators', {})
    
    # 必須データのチェック
    required_indicators = ['SMA_200', 'MACDh', 'RSI', 'ATR', 'BB_width']
    if any(np.isnan(indicators.get(i, np.nan)) for i in required_indicators):
        logging.warning(f"⚠️ {symbol} - {timeframe}: 必須のテクニカル指標が不足しています。スコアリングをスキップします。")
        return None
        
    latest_close = df['close'].iloc[-1]
    
    # ロングとショートの両方でスコアを計算し、最適な方を選択
    best_score_result = None
    
    for side in ['long', 'short']:
        temp_score = BASE_SCORE
        temp_details = {'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS}

        # 1. 長期トレンド一致/逆行 (SMA200) - 構造的優位性
        sma_200 = indicators.get('SMA_200')
        long_term_reversal_penalty_value = 0.0
        if not np.isnan(sma_200):
            if side == 'long' and latest_close > sma_200:
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            elif side == 'short' and latest_close < sma_200:
                long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY
            else:
                long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY
        
        temp_score += long_term_reversal_penalty_value
        temp_details['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
        
        # 2. MACDモメンタム一致
        macd_h = indicators.get('MACDh')
        macd_score = indicators.get('MACD_momentum_score', 0.0) # -1.0, 0.0, 1.0
        macd_penalty_value = 0.0
        if not np.isnan(macd_h):
            if side == 'long':
                # MACDヒストグラムがプラス圏、かつ傾きが上向き
                if macd_h > 0 and macd_score > 0:
                    macd_penalty_value = MACD_CROSS_PENALTY
                # MACDヒストグラムがマイナス圏で、傾きが上向き（転換初期）でも弱く加点
                elif macd_h <= 0 and macd_score > 0:
                     macd_penalty_value = MACD_CROSS_PENALTY / 2.0
                # 逆行している場合
                elif macd_score < 0:
                    macd_penalty_value = -MACD_CROSS_PENALTY
            
            elif side == 'short':
                # MACDヒストグラムがマイナス圏、かつ傾きが下向き
                if macd_h < 0 and macd_score < 0:
                    macd_penalty_value = MACD_CROSS_PENALTY
                # MACDヒストグラムがプラス圏で、傾きが下向き（転換初期）でも弱く加点
                elif macd_h >= 0 and macd_score < 0:
                     macd_penalty_value = MACD_CROSS_PENALTY / 2.0
                # 逆行している場合
                elif macd_score > 0:
                    macd_penalty_value = -MACD_CROSS_PENALTY
                    
        temp_score += macd_penalty_value
        temp_details['macd_penalty_value'] = macd_penalty_value

        # 3. RSIモメンタム加速 (買われ過ぎ/売られ過ぎからの適正水準への回帰)
        rsi = indicators.get('RSI')
        rsi_momentum_bonus_value = 0.0
        # 💡 RSI_14が存在するかチェック
        if 'RSI_14' in df.columns and not np.isnan(rsi):
            if side == 'long':
                # 売られ過ぎ水準(40以下)から回復し、適正水準(RSI 40〜60)に入った場合
                if rsi > RSI_MOMENTUM_LOW and rsi < 60 and df['RSI_14'].iloc[-2] <= RSI_MOMENTUM_LOW:
                    rsi_momentum_bonus_value = 0.05
            elif side == 'short':
                # 買われ過ぎ水準(60以上)から下落し、適正水準(RSI 40〜60)に入った場合
                if rsi < (100 - RSI_MOMENTUM_LOW) and rsi > 40 and df['RSI_14'].iloc[-2] >= (100 - RSI_MOMENTUM_LOW):
                    rsi_momentum_bonus_value = 0.05
        
        temp_score += rsi_momentum_bonus_value
        temp_details['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
        
        # 4. OBV出来高確証 (価格と出来高の方向一致)
        obv_momentum_score = indicators.get('OBV_momentum_score', 0.0)
        obv_momentum_bonus_value = 0.0
        if side == 'long' and obv_momentum_score > 0:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        elif side == 'short' and obv_momentum_score < 0:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS

        temp_score += obv_momentum_bonus_value
        temp_details['obv_momentum_bonus_value'] = obv_momentum_bonus_value
        
        # 5. 🆕 StochRSI (短期的な過熱感によるペナルティ/ボーナス)
        stoch_rsi_penalty_value = 0.0
        stoch_rsi_k = indicators.get('stoch_rsi_k', np.nan)
        stoch_rsi_d = indicators.get('stoch_rsi_d', np.nan)
        
        # StochRSIのK線とD線が両方とも極端な水準にある場合、短期的な反発リスクとしてペナルティを適用
        if not np.isnan(stoch_rsi_k) and not np.isnan(stoch_rsi_d):
            if side == 'long':
                # ロングシグナルだが、StochRSIが買われ過ぎ水準(80以上)にある場合、ペナルティ
                if stoch_rsi_k > (100 - STOCHRSI_BOS_LEVEL) and stoch_rsi_d > (100 - STOCHRSI_BOS_LEVEL):
                    stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY
                # ロングシグナルで、StochRSIが売られ過ぎ水準(20以下)から回復し始めた場合、弱めのボーナス
                elif stoch_rsi_k > STOCHRSI_BOS_LEVEL and stoch_rsi_d > STOCHRSI_BOS_LEVEL and (stoch_rsi_k < 50 or stoch_rsi_d < 50): 
                    stoch_rsi_penalty_value = STOCHRSI_BOS_PENALTY / 2.0 
            elif side == 'short':
                # ショートシグナルだが、StochRSIが売られ過ぎ水準(20以下)にある場合、ペナルティ
                if stoch_rsi_k < STOCHRSI_BOS_LEVEL and stoch_rsi_d < STOCHRSI_BOS_LEVEL:
                    stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY
                # ショートシグナルで、StochRSIが買われ過ぎ水準(80以上)から下落し始めた場合、弱めのボーナス
                elif stoch_rsi_k < (100 - STOCHRSI_BOS_LEVEL) and stoch_rsi_d < (100 - STOCHRSI_BOS_LEVEL) and (stoch_rsi_k > 50 or stoch_rsi_d > 50): 
                    stoch_rsi_penalty_value = STOCHRSI_BOS_PENALTY / 2.0 

        temp_score += stoch_rsi_penalty_value
        temp_details['stoch_rsi_penalty_value'] = stoch_rsi_penalty_value
        temp_details['stoch_rsi_k_value'] = stoch_rsi_k 

        # 6. 🆕 ADX (トレンドの強さの確証)
        adx_bonus_value = 0.0
        adx_value = indicators.get('adx', np.nan)
        adx_plus = indicators.get('adx_plus', np.nan)
        adx_minus = indicators.get('adx_minus', np.nan)
        
        if not np.isnan(adx_value) and adx_value >= ADX_TREND_STRENGTH_THRESHOLD:
            # ADXがトレンド閾値以上である場合、トレンドが強いと判断
            if side == 'long':
                # ロング: +DIが-DIを上回っていることを確認 (+DI > -DI)
                if adx_plus > adx_minus:
                    adx_bonus_value = ADX_TREND_BONUS 
            elif side == 'short':
                # ショート: -DIが+DIを上回っていることを確認 (-DI > +DI)
                if adx_minus > adx_plus:
                    adx_bonus_value = ADX_TREND_BONUS 

        temp_score += adx_bonus_value
        temp_details['adx_trend_bonus_value'] = adx_bonus_value
        temp_details['adx_raw_value'] = adx_value 


        # 7. ボラティリティ過熱ペナルティ (BB幅が価格に対して広すぎる場合)
        bb_width = indicators.get('BB_width')
        volatility_penalty_value = 0.0
        if not np.isnan(bb_width):
            # BB幅を価格に対する割合で評価
            bb_ratio = bb_width / latest_close
            if bb_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
                penalty = -(bb_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) * 10.0 
                volatility_penalty_value = max(-0.10, penalty)
        
        temp_score += volatility_penalty_value
        temp_details['volatility_penalty_value'] = volatility_penalty_value
        
        
        # 8. FGIマクロ影響
        fgi_proxy = macro_context.get('fgi_proxy', 0.0)
        sentiment_fgi_proxy_bonus = 0.0
        if side == 'long' and fgi_proxy > 0:
            sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, fgi_proxy)
        elif side == 'short' and fgi_proxy < 0:
            sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, abs(fgi_proxy))
        elif side == 'long' and fgi_proxy < 0:
            sentiment_fgi_proxy_bonus = max(-FGI_PROXY_BONUS_MAX, fgi_proxy)
        elif side == 'short' and fgi_proxy > 0:
            sentiment_fgi_proxy_bonus = max(-FGI_PROXY_BONUS_MAX, -fgi_proxy)
            
        temp_score += sentiment_fgi_proxy_bonus
        temp_details['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
        
        # 9. 流動性ボーナス
        liquidity_bonus_value = 0.0
        if liquidity_rank > 0 and liquidity_rank <= TOP_SYMBOL_LIMIT:
            liquidity_bonus_value = LIQUIDITY_BONUS_MAX * (1.0 - (liquidity_rank - 1) / TOP_SYMBOL_LIMIT)
            
        temp_score += liquidity_bonus_value
        temp_details['liquidity_bonus_value'] = liquidity_bonus_value
        
        
        # 最終スコアの計算と丸め (最大値1.00)
        final_score = min(1.0, temp_score)
        
        # 最もスコアの高いシグナルを選択
        if best_score_result is None or final_score > best_score_result['score']:
            best_score_result = {
                'symbol': symbol,
                'timeframe': timeframe,
                'side': side,
                'score': final_score,
                'tech_data': temp_details
            }
            
    return best_score_result

# ====================================================================================
# CORE LOGIC
# ====================================================================================

async def fetch_top_volume_tickers(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所から出来高上位の銘柄を取得する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 出来高銘柄取得失敗: CCXTクライアントが準備できていません。")
        return DEFAULT_SYMBOLS
        
    try:
        # USDT建ての先物市場のみをフィルタリング
        future_markets = {
            s: m for s, m in EXCHANGE_CLIENT.markets.items() 
            if m.get('quote') == 'USDT' and m.get('type') in ['future', 'swap'] and m.get('active')
        }
        
        # 💡 修正: fetch_tickersがNoneを返す可能性に対処
        tickers_data = await EXCHANGE_CLIENT.fetch_tickers(list(future_markets.keys()))
        if tickers_data is None:
             logging.warning("⚠️ fetch_tickersがNoneを返しました。デフォルト銘柄リストを使用します。")
             return DEFAULT_SYMBOLS

        # 出来高 (quoteVolume) を基準にソート
        # 'quoteVolume'がない場合は'volume'を、それもない場合は0を代替値とする
        tickers_list = list(tickers_data.values())
        tickers_list.sort(key=lambda t: t.get('quoteVolume') or t.get('volume') or 0, reverse=True)
        
        # 上位N件を取得
        top_symbols = [t['symbol'] for t in tickers_list if t['symbol'].endswith('/USDT')][:limit]
        
        # デフォルト銘柄のうち、まだ追加されていないものを追加 (重要銘柄の漏れを防ぐ)
        for d_symbol in DEFAULT_SYMBOLS:
            if d_symbol not in top_symbols and d_symbol in future_markets.keys():
                top_symbols.append(d_symbol)

        # 最終的なリストをCCXT標準シンボル形式(BTC/USDT)で返す
        logging.info(f"✅ 出来高上位銘柄 ({len(top_symbols)}件) を取得しました。")
        return top_symbols
        
    except ccxt.ExchangeNotAvailable as e:
        logging.critical(f"❌ 出来高銘柄取得失敗 - 取引所接続エラー: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"❌ 出来高銘柄取得中に予期せぬエラー: {e}", exc_info=True)
        
    return DEFAULT_SYMBOLS

async def fetch_macro_context() -> Dict:
    """
    FGI (Fear & Greed Index) などのマクロ環境データを取得する。
    """
    global GLOBAL_MACRO_CONTEXT
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        
        fgi_value = int(data['data'][0]['value']) # 0-100
        
        # FGIをプロキシスコア (-0.50から+0.50) に変換
        # 50 = 0.0, 0 = -0.50, 100 = +0.50
        fgi_proxy = (fgi_value - 50) / 100 
        
        # FGIの生の値とテキスト
        fgi_raw_value = f"{fgi_value} ({data['data'][0]['value_classification']})"
        
        # 外国為替ボーナスは実装されていないため 0.0
        forex_bonus = 0.0

        GLOBAL_MACRO_CONTEXT = {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': fgi_raw_value,
            'forex_bonus': forex_bonus
        }
        
        logging.info(f"✅ マクロ環境 (FGI) を更新しました: {fgi_raw_value} (Proxy: {fgi_proxy:+.2f})")
        
    except Exception as e:
        logging.error(f"❌ マクロ環境データの取得に失敗しました: {e}")
        # 取得失敗時はデフォルト値を使用
        GLOBAL_MACRO_CONTEXT = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
        
    return GLOBAL_MACRO_CONTEXT

async def check_for_signals(symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    全ての監視銘柄と時間足でシグナルをチェックし、スコア付けを行う。
    """
    all_signals = []
    
    # 出来高ランク付け (流動性ボーナス用)
    liquidity_rank_map = {s: i + 1 for i, s in enumerate(symbols)}
    
    tasks = []
    for symbol in symbols:
        for timeframe in TARGET_TIMEFRAMES:
            # 必要なOHLCVデータ量のチェック
            required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            
            async def process_symbol_timeframe(s, tf, req_limit, rank, m_context):
                df = await fetch_historical_ohlcv(s, tf, req_limit)
                if df is not None:
                    # 💡 修正: calculate_indicatorsがDFとDictを返すように変更したため、受け取り側も修正
                    df_mod, indicators = await calculate_indicators(s, tf, df)
                    
                    if indicators is not None:
                        # calculate_trade_scoreには更新されたdf_modを渡す
                        signal = calculate_trade_score(s, tf, {'indicators': indicators}, df_mod, m_context, rank)
                        if signal:
                            return signal
                return None
            
            tasks.append(process_symbol_timeframe(symbol, timeframe, required_limit, liquidity_rank_map.get(symbol, TOP_SYMBOL_LIMIT), macro_context))
    
    results = await asyncio.gather(*tasks)
    
    # Noneでない結果のみをフィルタリング
    all_signals = [r for r in results if r is not None]
    
    logging.info(f"✅ 全分析を完了しました。合計 {len(all_signals)} 件のシグナル候補が見つかりました。")
    return all_signals

def find_best_signals(all_signals: List[Dict], macro_context: Dict) -> List[Dict]:
    """
    全シグナルの中から、現在の市場環境に基づき、最適なシグナルをフィルタリングする。
    """
    global LAST_SIGNAL_TIME
    
    current_time = time.time()
    
    # 1. 現在の動的な取引閾値を決定
    current_threshold = get_current_threshold(macro_context)
    
    # 2. 閾値を超えたシグナルのみをフィルタリング
    filtered_signals = [s for s in all_signals if s['score'] >= current_threshold]
    
    # 3. クールダウン期間中の銘柄を除外
    tradeable_signals = []
    for signal in filtered_signals:
        symbol = signal['symbol']
        last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0)
        
        if current_time - last_trade_time > TRADE_SIGNAL_COOLDOWN:
            tradeable_signals.append(signal)
        else:
            cooldown_remaining = (last_trade_time + TRADE_SIGNAL_COOLDOWN) - current_time
            logging.info(f"🕒 {symbol} はクールダウン中 (残り {cooldown_remaining/3600:.1f} 時間)。取引をスキップします。")
            
    # 4. スコアの高い順にソートし、TOP_SIGNAL_COUNTに絞る
    tradeable_signals.sort(key=lambda x: x['score'], reverse=True)
    
    final_signals = tradeable_signals[:TOP_SIGNAL_COUNT]
    
    logging.info(f"✅ 最終的に {len(final_signals)} 件の最適な取引シグナルが選定されました (閾値: {current_threshold*100:.2f}点)。")
    return final_signals

# ====================================================================================
# POSITION MANAGEMENT & TRADING EXECUTION
# ====================================================================================

async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    """
    シグナルに基づき、取引所に対して新規エントリー注文を行う。
    """
    global EXCHANGE_CLIENT, LAST_SIGNAL_TIME
    
    symbol = signal['symbol']
    side = signal['side']
    # ロングは 'buy'、ショートは 'sell' の成行注文でエントリー
    order_side = 'buy' if side == 'long' else 'sell'
    
    # 1. リスク管理パラメータの計算
    risk_params = manage_trade_risk(signal)
    if risk_params is None:
        return {'status': 'error', 'error_message': 'リスクパラメータ計算失敗。'}
        
    entry_price = risk_params['entry_price']
    stop_loss = risk_params['stop_loss']
    take_profit = risk_params['take_profit']
    contracts = risk_params['contracts']
    
    # 2. 注文実行の準備
    if TEST_MODE:
        # テストモードでは注文をスキップし、成功したかのようにデータを構築
        trade_result = {
            'status': 'ok',
            'filled_amount': contracts,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side} のエントリー注文をシミュレートしました。")
        
    else:
        # 実際の注文実行
        try:
            # 成行注文
            order = await EXCHANGE_CLIENT.create_market_order(
                symbol, 
                order_side, 
                contracts,
                params={
                    'type': TRADE_TYPE, 
                    'leverage': LEVERAGE,
                    # MEXCの場合、positionTypeが必要
                    'positionSide': side.capitalize() 
                }
            )
            
            # 注文結果の確認 (ccxtの仕様に依存するため簡略化)
            filled_amount = float(order.get('filled', 0.0) or contracts)
            
            if filled_amount > 0 and order.get('status') in ['closed', 'fill', 'filled', 'open']:
                # 約定した価格 (last_fill_priceやaverageから取得)
                exec_price = order.get('price') or order.get('average', entry_price) 
                
                trade_result = {
                    'status': 'ok',
                    'filled_amount': filled_amount,
                    # 名目価値の再計算 (約定価格と数量を使用)
                    'filled_usdt': filled_amount * exec_price, 
                    'entry_price': exec_price, 
                }
                
                # SL/TPはポジション情報に後で反映させるため、ここでは実行価格のみを更新
                entry_price = exec_price
                logging.info(f"✅ {symbol} ({side}) エントリー注文成功。数量: {filled_amount:.4f}, 価格: {format_price(entry_price)}")
            else:
                raise Exception(f"約定数量が0、または注文ステータス異常: {order.get('status')}")
                
        except ccxt.ExchangeError as e:
            error_message = f"取引所エラー: {e}"
            trade_result = {'status': 'error', 'error_message': error_message}
            logging.error(f"❌ {symbol} エントリー注文失敗: {error_message}")
        except Exception as e:
            error_message = f"予期せぬエラー: {e}"
            trade_result = {'status': 'error', 'error_message': error_message}
            logging.error(f"❌ {symbol} エントリー注文失敗: {error_message}", exc_info=True)


    if trade_result.get('status') == 'ok':
        # 成功した場合、シグナル情報と取引結果を結合し、ポジションリストに追加
        final_signal = {
            **signal, 
            **risk_params, # SL, TP, RRRを上書き
            'entry_price': entry_price, # 実際の約定価格で上書き
            'contracts': trade_result['filled_amount'],
            'filled_usdt': trade_result['filled_usdt'],
            'liquidation_price': calculate_liquidation_price(entry_price, LEVERAGE, side),
            'timestamp': int(time.time() * 1000)
        }
        
        # グローバルポジションリストの更新 (メインループで最新化されるまでの暫定対応)
        OPEN_POSITIONS.append(final_signal) 
        LAST_SIGNAL_TIME[symbol] = time.time() # クールダウンタイマー開始
        
        # 通知とログ
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        await send_telegram_notification(format_telegram_message(final_signal, "取引シグナル", current_threshold, trade_result=trade_result))
        log_signal(final_signal, "取引実行", trade_result)
        
        return trade_result
        
    return trade_result


def manage_trade_risk(signal: Dict) -> Optional[Dict]:
    """
    ATRに基づき、エントリー価格、SL、TPを計算する。
    """
    
    indicators = signal['tech_data']['indicators']
    latest_close = signal.get('entry_price') # 最新の終値/エントリー価格
    side = signal['side']
    
    if latest_close is None or latest_close <= 0:
        return None
        
    atr = indicators.get('ATR')
    if atr is None or np.isnan(atr) or atr <= 0:
        return None
        
    # 1. SLの計算 (ATR_MULTIPLIER_SL * ATR)
    risk_distance = ATR_MULTIPLIER_SL * atr
    
    # 最低リスク幅の適用 (価格 * MIN_RISK_PERCENT)
    min_risk_distance = latest_close * MIN_RISK_PERCENT
    risk_distance = max(risk_distance, min_risk_distance) # より広い方を採用

    if side == 'long':
        stop_loss = latest_close - risk_distance
        take_profit = latest_close + (risk_distance * RR_RATIO_TARGET)
    else: # short
        stop_loss = latest_close + risk_distance
        take_profit = latest_close - (risk_distance * RR_RATIO_TARGET)
        
    # SL/TPが0以下にならないように保護
    if stop_loss <= 0 or take_profit <= 0:
        return None
        
    # 2. ポジションサイズの計算 (固定名目価値)
    # contracts = FIXED_NOTIONAL_USDT / entry_price
    contracts = FIXED_NOTIONAL_USDT / latest_close
    
    return {
        'entry_price': latest_close, # 暫定エントリー価格
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'contracts': contracts,
        'rr_ratio': RR_RATIO_TARGET,
    }


async def process_exit_signal(position: Dict, exit_type: str, close_price: float) -> Optional[Dict]:
    """
    ポジションを決済し、結果を返す。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    contracts = abs(position['contracts'])
    entry_price = position['entry_price']
    
    # ショートは 'buy' (買い戻し)、ロングは 'sell' (売り) の成行注文で決済
    order_side = 'buy' if side == 'short' else 'sell'
    
    trade_result = {}
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} - {side} の決済 ({exit_type}) をシミュレートしました。")
        # PnL計算
        pnl_rate = ((close_price - entry_price) / entry_price) * LEVERAGE * (1 if side == 'long' else -1)
        pnl_usdt = position['filled_usdt'] * pnl_rate
        
        trade_result = {
            'status': 'ok',
            'exit_price': close_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'entry_price': entry_price,
            'filled_amount': contracts,
            'exit_type': exit_type
        }
    
    else:
        try:
            # 決済注文の実行
            order = await EXCHANGE_CLIENT.create_market_order(
                symbol, 
                order_side, 
                contracts,
                params={
                    'reduceOnly': True, # 必ず reduceOnly を設定
                    'positionSide': side.capitalize()
                }
            )
            
            # 約定価格の取得 (order.get('average')またはorder.get('price'))
            exec_price = order.get('average') or order.get('price') or close_price
            
            # PnLの計算 (ccxtで取得できない場合は手動で計算)
            # 💡 実際の取引所では PnL は取引所のAPIから取得するのが確実だが、ここでは概算で対応
            pnl_rate = ((exec_price - entry_price) / entry_price) * LEVERAGE * (1 if side == 'long' else -1)
            pnl_usdt = position['filled_usdt'] * pnl_rate
            
            trade_result = {
                'status': 'ok',
                'exit_price': exec_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'entry_price': entry_price,
                'filled_amount': contracts,
                'exit_type': exit_type
            }
            logging.info(f"✅ {symbol} ({side}) 決済注文成功 ({exit_type})。価格: {format_price(exec_price)}, PnL: {format_usdt(pnl_usdt)}")

        except Exception as e:
            error_message = f"決済注文失敗: {e}"
            trade_result = {'status': 'error', 'error_message': error_message, 'exit_type': exit_type}
            logging.error(f"❌ {symbol} 決済注文失敗: {error_message}")
            return None # 失敗時はポジション管理を続行させるため None を返す

    # 成功した場合
    if trade_result.get('status') == 'ok':
        # グローバルポジションリストから削除
        global OPEN_POSITIONS
        try:
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p.get('symbol') != symbol or p.get('side') != side]
        except Exception:
            # 安全のため、シンボルだけ削除を試みる
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p.get('symbol') != symbol]
            
        # 通知とログ
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 通知用
        await send_telegram_notification(format_telegram_message(position, "ポジション決済", current_threshold, trade_result=trade_result, exit_type=exit_type))
        log_signal(position, "ポジション決済", trade_result)
        
        return trade_result
        
    return None # 決済失敗


async def check_and_manage_open_positions():
    """
    現在のオープンポジションをチェックし、SL/TPに達しているか確認する。
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    tasks = []
    # リアルタイムの価格データを取得するためのティッカーリスト
    symbols_to_fetch = [p['symbol'] for p in OPEN_POSITIONS]
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション管理失敗: CCXTクライアントが準備できていません。")
        return
        
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
    except Exception as e:
        logging.error(f"❌ ポジション管理に必要なティッカー価格の取得に失敗しました: {e}")
        return
        
    # SL/TPのチェックと管理 (非同期で実行)
    for position in OPEN_POSITIONS:
        tasks.append(manage_position_sl_tp(position, tickers.get(position['symbol'])))
        
    await asyncio.gather(*tasks)


async def manage_position_sl_tp(position: Dict, ticker: Optional[Dict]):
    """
    個別のポジションについてSL/TPをチェックし、必要であれば決済を行う。
    """
    symbol = position['symbol']
    side = position['side']
    sl = position.get('stop_loss', 0.0)
    tp = position.get('take_profit', 0.0)
    
    if ticker is None or 'last' not in ticker:
        logging.warning(f"⚠️ {symbol} の最新価格データが取得できませんでした。スキップします。")
        return

    # 最新の市場価格
    current_price = ticker['last']
    
    exit_type: Optional[str] = None
    
    if side == 'long':
        if sl > 0 and current_price <= sl:
            exit_type = "SL (損切り) ヒット"
        elif tp > 0 and current_price >= tp:
            exit_type = "TP (利益確定) ヒット"
    elif side == 'short':
        if sl > 0 and current_price >= sl:
            exit_type = "SL (損切り) ヒット"
        elif tp > 0 and current_price <= tp:
            exit_type = "TP (利益確定) ヒット"
            
    if exit_type:
        await process_exit_signal(position, exit_type, current_price)


# ====================================================================================
# SCHEDULERS & ENTRY POINT
# ====================================================================================

async def main_bot_scheduler():
    """
    メインの分析・取引スケジューラ。
    """
    # 💡 修正: LAST_WEBSHARE_UPLOAD_TIME を global 宣言に追加
    global CURRENT_MONITOR_SYMBOLS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS, \
           LAST_SUCCESS_TIME, ACCOUNT_EQUITY_USDT, LAST_HOURLY_NOTIFICATION_TIME, \
           LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_WEBSHARE_UPLOAD_TIME
    
    logging.info("⏳ メインボットスケジューラを開始します。")
    
    # 1. CCXTクライアントの初期化
    if not await initialize_exchange_client():
        # クライアント初期化失敗は致命的
        await send_telegram_notification("❌ **致命的エラー**: CCXTクライアントの初期化に失敗しました。BOTを停止します。")
        return
        
    # 2. 初回マクロ環境の取得
    macro_context = await fetch_macro_context()
    
    # 3. 初回口座ステータスの取得と起動通知
    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(macro_context)
    await send_telegram_notification(format_startup_message(account_status, macro_context, len(CURRENT_MONITOR_SYMBOLS), current_threshold))
    
    
    while True:
        try:
            start_time = time.time()
            current_time = start_time # 実行開始時刻を現在の時刻として使用
            
            # --- データ更新フェーズ ---
            
            # 出来高上位銘柄の更新 (レートリミット対策のため頻度は抑える)
            if not IS_FIRST_MAIN_LOOP_COMPLETED or current_time - LAST_SUCCESS_TIME > (60 * 30): # 30分に1回更新
                if not SKIP_MARKET_UPDATE:
                    CURRENT_MONITOR_SYMBOLS = await fetch_top_volume_tickers(TOP_SYMBOL_LIMIT)
                else:
                    logging.warning("⚠️ SKIP_MARKET_UPDATEが有効です。銘柄リストの更新をスキップします。")
            
            # マクロ環境の更新
            macro_context = await fetch_macro_context()
            current_threshold = get_current_threshold(macro_context)
            
            # 口座ステータスの更新
            await fetch_account_status()
            
            # --- 分析フェーズ ---
            
            # 全監視銘柄に対してシグナルチェックとスコアリングを実行
            all_signals = await check_for_signals(CURRENT_MONITOR_SYMBOLS, macro_context)
            LAST_ANALYSIS_SIGNALS = all_signals # 定期通知のために保存
            
            # --- 取引判断フェーズ ---
            
            # 最適な取引シグナルを選定
            best_signals = find_best_signals(all_signals, macro_context)
            
            # --- 取引実行フェーズ ---
            
            trade_tasks = []
            for signal in best_signals:
                # すでにポジションを持っている銘柄はスキップ
                if not any(p['symbol'] == signal['symbol'] and p['side'] == signal['side'] for p in OPEN_POSITIONS):
                    trade_tasks.append(process_entry_signal(signal))
                else:
                    logging.info(f"ℹ️ {signal['symbol']} ({signal['side']}) のポジションは既に保有しています。エントリーをスキップします。")
                    
            if trade_tasks:
                await asyncio.gather(*trade_tasks)
            
            # 💡 定期通知の実行
            await notify_highest_analysis_score() 

            # --- WebShareアップロード ---
            # 💡 修正: global宣言されたため、変数参照が可能に
            if current_time - LAST_WEBSHARE_UPLOAD_TIME > WEBSHARE_UPLOAD_INTERVAL:
                 webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'open_positions': OPEN_POSITIONS, 
                    'macro_context': macro_context,
                    'last_signals': best_signals # 最後に選定されたシグナル
                 }
                 await send_webshare_update(webshare_data)
                 LAST_WEBSHARE_UPLOAD_TIME = current_time
                 
            
            # --- ループ完了と待機 ---
            end_time = time.time()
            elapsed_time = end_time - start_time
            sleep_duration = max(0, LOOP_INTERVAL - elapsed_time)
            
            logging.info(f"✅ メイン分析ループ完了。所要時間: {elapsed_time:.2f}秒。次回実行まで {sleep_duration:.2f}秒待機します。")
            
            LAST_SUCCESS_TIME = start_time
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            await asyncio.sleep(sleep_duration)
            
        except ccxt.RateLimitExceeded as e:
            logging.error(f"❌ レート制限エラー。1分間スリープします。: {e}")
            await asyncio.sleep(60) 
        except Exception as e:
            logging.critical(f"❌ メインボットスケジューラで予期せぬ致命的エラーが発生しました: {e}", exc_info=True)
            # エラーが発生してもボットは継続させる
            await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """
    オープンポジションのSL/TPを高速で監視するスケジューラ。
    """
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
    
    if "Unclosed" not in str(exc):
        logging.error(f"❌ 捕捉されなかった例外: {exc}", exc_info=True)

    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )
