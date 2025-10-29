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
    stoch_k = indicators.get('STOCHRSIk', np.nan)
    
    if stoch_val < 0:
        breakdown_list.append(f"🔴 StochRSI過熱ペナルティ (K={stoch_k:.1f}): {stoch_val*100:+.2f} 点")
    elif stoch_val > 0:
        breakdown_list.append(f"🟢 StochRSI過熱回復ボーナス (K={stoch_k:.1f}): {stoch_val*100:+.2f} 点")

    # 🆕 ADXトレンド確証
    adx_val = tech_data.get('adx_trend_bonus_value', 0.0)
    adx_raw = indicators.get('ADX', np.nan)
    
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
        logging.critical(f"❌ CCXTクライアントの初期化中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return False

async def fetch_tickers_and_update_symbols() -> List[str]:
    """
    取引所のティッカー情報を取得し、出来高上位銘柄に監視シンボルリストを更新する。
    """
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.warning("⚠️ クライアント未準備、または市場更新をスキップする設定です。デフォルトシンボルを使用します。")
        return CURRENT_MONITOR_SYMBOLS

    try:
        # MEXCの場合、USDT建てのSwap/Future市場のみをフィルタリング
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not isinstance(tickers, dict):
             logging.error("❌ fetch_tickersの戻り値が不正な型です (dictではありません)。")
             return CURRENT_MONITOR_SYMBOLS
             
        if not tickers.keys():
            logging.error("❌ fetch_tickersの結果が空です。")
            return CURRENT_MONITOR_SYMBOLS
            
        usdt_futures = [
            t for t in tickers.values() 
            if t['symbol'].endswith('/USDT') and 
               t['info'].get('type') in ['swap', 'future'] and 
               t['info'].get('isInverse', False) == False and # インバース契約ではない
               t.get('quoteVolume') is not None # 出来高が存在すること
        ]
        
        # quoteVolume (USDT出来高) の降順でソート
        usdt_futures.sort(key=lambda t: t['quoteVolume'], reverse=True)
        
        # TOP N 銘柄と、デフォルトの重要銘柄を結合
        top_symbols = [t['symbol'] for t in usdt_futures[:TOP_SYMBOL_LIMIT]]
        
        # デフォルトシンボルから、既にトップリストにあるものを除き、マージ
        merged_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        # CCXTシンボル形式 (例: BTC/USDT) に統一されていることを確認
        CURRENT_MONITOR_SYMBOLS = [s for s in merged_symbols if '/' in s]
        
        logging.info(f"✅ 出来高ランキング更新完了。監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)} (TOP {TOP_SYMBOL_LIMIT} + Default)")
        return CURRENT_MONITOR_SYMBOLS
        
    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ DDoS保護が作動しました。レートリミット超過の可能性があります。: {e}")
    except Exception as e:
        logging.error(f"❌ ティッカー情報の取得中にエラーが発生しました: {e}", exc_info=True)
        
    return CURRENT_MONITOR_SYMBOLS


async def fetch_ohlcv(symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """指定した銘柄・時間枠のOHLCVデータを取得する。"""
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return None
        
    try:
        # CCXTのfetch_ohlcvはリストを返す
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが空です。")
            return None

        # pandas DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # タイムスタンプを datetime (JST) に変換
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # 必要な本数未満の場合は警告
        if len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のデータ本数が {len(df)} 本と、必要本数 {limit} 本を下回っています。")
            
        return df
        
    except ccxt.ExchangeError as e:
        logging.error(f"❌ {symbol} のOHLCVデータ取得中に取引所エラーが発生: {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} のOHLCVデータ取得中に予期せぬエラーが発生: {e}", exc_info=True)
        
    return None

async def get_account_status() -> Dict:
    """
    現在の先物口座の残高とポジション情報を取得し、グローバル変数を更新する。
    """
    global ACCOUNT_EQUITY_USDT, OPEN_POSITIONS, EXCHANGE_CLIENT
    
    ACCOUNT_EQUITY_USDT = 0.0
    OPEN_POSITIONS = []
    
    if not EXCHANGE_CLIENT:
        return {'error': True, 'message': 'CCXTクライアントが初期化されていません。'}
        
    # mexcは fetch_balance() で先物 (swap) の残高が取れない場合があるため、一旦ダミーで返す
    if EXCHANGE_CLIENT.id == 'mexc':
        logging.warning("⚠️ MEXCはCCXTの制約により正確な残高情報取得が難しい場合があります。ダミー値を使用します。")
        return {
            'error': False,
            'total_usdt_balance': 0.0,
            'margin_balance': 0.0,
            'info': {'備考': 'CCXTの制約 (mexc) により正確な残高情報取得不可。'}
        }
        
    try:
        # 残高情報の取得
        balance = await EXCHANGE_CLIENT.fetch_balance({'type': 'future'})
        total_usdt = balance.get('total', {}).get('USDT', 0.0)
        margin_balance = balance.get('info', {}).get('margin_balance', total_usdt)
        
        ACCOUNT_EQUITY_USDT = total_usdt
        
        # ポジション情報の取得
        positions = await EXCHANGE_CLIENT.fetch_positions(symbols=None, params={'type': 'future'})
        
        # アクティブなポジションのみをフィルタリング
        active_positions = []
        for p in positions:
            # size > 0 または contract > 0 の場合をアクティブと見なす
            # CCXTの仕様により、'size'または'contract'のどちらかを使う
            position_size = p.get('contracts', p.get('contract', p.get('size', 0.0)))
            
            if position_size != 0.0 and p.get('marginMode', 'cross').lower() == 'cross':
                
                # ポジションの情報を整形して保存 (BOT管理に必要な情報のみ)
                active_positions.append({
                    'id': p.get('id', str(uuid.uuid4())), # ポジションIDまたはユニークID
                    'symbol': p['symbol'],
                    'side': p['side'].lower(),
                    'contracts': abs(position_size), # 契約数 (絶対値)
                    'entry_price': p.get('entryPrice', 0.0),
                    'liquidation_price': p.get('liquidationPrice', 0.0),
                    'leverage': p.get('leverage', LEVERAGE),
                    'unrealizedPnl': p.get('unrealizedPnl', 0.0),
                    'initialMargin': p.get('initialMargin', 0.0),
                    # BOTで設定した SL/TP (CCXTからは通常取得できないため、BOTで管理)
                    'stop_loss': 0.0,
                    'take_profit': 0.0,
                    'filled_usdt': abs(position_size * p.get('entryPrice', 0.0) / p.get('leverage', LEVERAGE))
                })

        OPEN_POSITIONS = active_positions
        
        logging.info(f"✅ 口座ステータス更新完了: 総資産 {format_usdt(total_usdt)} USDT, 管理中ポジション数: {len(OPEN_POSITIONS)}")
        
        return {
            'error': False,
            'total_usdt_balance': total_usdt,
            'margin_balance': margin_balance,
            'info': balance.get('info')
        }
        
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得中にネットワークエラーが発生しました: {e}", exc_info=True)
    except ccxt.ExchangeError as e:
        # 認証失敗、権限不足、サーバーエラーなどの場合
        logging.error(f"❌ 口座ステータス取得中に取引所エラーが発生しました: {e}", exc_info=True)
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return {'error': True, 'message': '口座ステータスの取得に失敗しました。'}


async def fetch_macro_context() -> Dict:
    """
    外部APIから市場全体のマクロコンテキスト情報 (FGI) を取得し、スコアリング用のプロキシ値を計算する。
    """
    global GLOBAL_MACRO_CONTEXT
    
    try:
        # 1. 恐怖・貪欲指数 (FGI) を取得
        fgi_response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        fgi_response.raise_for_status()
        fgi_data = fgi_response.json()
        
        fgi_value = int(fgi_data['data'][0]['value']) # 0 (Extreme Fear) - 100 (Extreme Greed)
        
        # FGIプロキシ値の計算: 中央値50を0とし、標準化
        # プロキシ値の範囲は -0.5 から +0.5
        fgi_proxy = (fgi_value - 50) / 100
        
        # 2. 為替影響ボーナス (ダミー/N/A)
        # 実際の実装では、USDインデックス (DXY) の動きなどから決定
        forex_bonus = 0.0
        
        GLOBAL_MACRO_CONTEXT = {
            'fgi_proxy': fgi_proxy,
            'fgi_raw_value': f"{fgi_value} ({fgi_data['data'][0]['value_classification']})",
            'forex_bonus': forex_bonus,
            'last_update': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        }
        logging.info(f"✅ マクロコンテキスト更新: FGI={fgi_value}, Proxy={fgi_proxy:.2f}")
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.warning(f"⚠️ FGI取得APIへの接続エラー: {e}。前回値 ({GLOBAL_MACRO_CONTEXT['fgi_raw_value']}) を使用します。")
    except Exception as e:
        logging.error(f"❌ マクロコンテキストの取得中に予期せぬエラー: {e}", exc_info=True)
        
    return GLOBAL_MACRO_CONTEXT


# ====================================================================================
# CORE TRADING LOGIC (ANALYSIS & SCORING)
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    指定されたOHLCVデータに基づき、必要なテクニカル指標を計算し、辞書で返す。
    """
    
    if df is None or df.empty or len(df) < 200:
        return {'error': True, 'message': 'データ不足。'}
        
    indicators = {}
    
    try:
        # MACD (12, 26, 9)
        macd_data = ta.macd(df['close'])
        if macd_data is not None and not macd_data.empty:
            indicators.update({
                'MACD': macd_data.iloc[-1].get('MACD_12_26_9'),
                'MACDh': macd_data.iloc[-1].get('MACDh_12_26_9'),
                'MACDs': macd_data.iloc[-1].get('MACDs_12_26_9'),
            })

        # RSI (14)
        rsi_data = ta.rsi(df['close'], length=14)
        if rsi_data is not None and not rsi_data.empty:
            indicators['RSI'] = rsi_data.iloc[-1]
            
        # SMA (200) - 長期トレンド判断用
        sma_data = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
        if sma_data is not None and not sma_data.empty:
            indicators['SMA200'] = sma_data.iloc[-1]
            
        # ボリンジャーバンド (BBANDS, 5, 2.0)
        bbands_data = ta.bbands(df['close'], length=5, std=2.0)
        if bbands_data is not None and not bbands_data.empty:
            latest_bb = bbands_data.iloc[-1]
            latest_close = df['close'].iloc[-1]
            
            # BB幅 (BBU - BBL)
            bb_width = latest_bb.get('BBU_5_2.0_2.0') - latest_bb.get('BBL_5_2.0_2.0')
            
            indicators.update({
                'BB_width': bb_width,
                'BBP': latest_bb.get('BBP_5_2.0_2.0'),
            })

        # ATR (14)
        atr_data = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
        if atr_data is not None and not atr_data.empty:
            indicators['ATR'] = atr_data.iloc[-1]
            
        # OBV (On Balance Volume)
        obv_data = ta.obv(df['close'], df['volume'])
        if obv_data is not None and not obv_data.empty:
            # 最新と一つ前のOBVを保存
            indicators['OBV_latest'] = obv_data.iloc[-1]
            indicators['OBV_prev'] = obv_data.iloc[-2] if len(obv_data) >= 2 else np.nan

        # 🆕 StochRSI (14, 14, 3, 3)
        stochrsi_data = ta.stochrsi(df['close'])
        if stochrsi_data is not None and not stochrsi_data.empty:
             indicators.update({
                'STOCHRSIk': stochrsi_data.iloc[-1].get('STOCHRSIk_14_14_3_3'),
                'STOCHRSId': stochrsi_data.iloc[-1].get('STOCHRSId_14_14_3_3'),
            })
            
        # 🆕 ADX / DMI (14)
        adx_data = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_data is not None and not adx_data.empty:
             indicators.update({
                'ADX': adx_data.iloc[-1].get('ADX_14'),
                'DMP': adx_data.iloc[-1].get('DMP_14'), # +DI
                'DMN': adx_data.iloc[-1].get('DMN_14'), # -DI
            })


        indicators['error'] = False
        indicators['latest_close'] = df['close'].iloc[-1]
        
    except Exception as e:
        logging.error(f"❌ テクニカル指標の計算中にエラーが発生しました: {e}", exc_info=True)
        indicators = {'error': True, 'message': f'指標計算エラー: {e}'}
        
    return indicators


def score_long_signal(indicators: Dict, latest_close: float, symbol: str, macro_context: Dict) -> Tuple[float, Dict]:
    """ロングシグナルをスコアリングし、詳細なスコア内訳を返す。"""
    
    if indicators.get('error'):
        return 0.0, {'score': 0.0, 'message': indicators.get('message', '指標計算エラー')}
        
    score = BASE_SCORE # ベーススコア 40点
    tech_data = {}
    
    # --- 1. 長期トレンドと価格構造 (SMA200) ---
    long_term_reversal_penalty_value = 0.0
    # 価格がSMA200の上にあること (順張り優位性)
    if not np.isnan(indicators.get('SMA200')) and latest_close > indicators['SMA200']:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # +0.20
    else:
        # 価格がSMA200の下にある場合 (逆張り/レンジ判定)
        long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY / 2 # -0.10 (ペナルティを半分に軽減)

    score += long_term_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # --- 2. 構造的優位性 (ベース) ---
    score += STRUCTURAL_PIVOT_BONUS # +0.05
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # --- 3. MACDモメンタム (上昇トレンド確証) ---
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインを上回っている AND MACDヒストグラムが増加傾向
    if not np.isnan(indicators.get('MACD')) and not np.isnan(indicators.get('MACDs')):
        if indicators['MACD'] > indicators['MACDs']: # MACDがゴールデンクロス状態
            # ヒストグラムがゼロラインより上で増加傾向 (強い上昇モメンタム)
            if indicators.get('MACDh', 0.0) > 0.0:
                 macd_penalty_value = MACD_CROSS_PENALTY # +0.15 (強いボーナス)
            else:
                 # MACD > MACDs だが、ヒストグラムは縮小傾向 (モメンタム失速)
                 macd_penalty_value = MACD_CROSS_PENALTY / 2 # +0.075 (弱いボーナス)
        else:
            # MACDがデッドクロス状態 (下降モメンタム)
            macd_penalty_value = -MACD_CROSS_PENALTY # -0.15 (強いペナルティ)

    score += macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # --- 4. RSIモメンタム加速 ---
    rsi_momentum_bonus_value = 0.0
    # RSIがRSI_MOMENTUM_LOW (40) を超えて上昇している (下降トレンドからの脱却)
    if not np.isnan(indicators.get('RSI')) and indicators['RSI'] > RSI_MOMENTUM_LOW: 
        # RSIが高いほど、モメンタムが強い
        rsi_momentum_bonus_value = min(RSI_DIVERGENCE_BONUS, (indicators['RSI'] - RSI_MOMENTUM_LOW) / 100) # Max +0.10

    score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # --- 5. 出来高確証 (OBV) ---
    obv_momentum_bonus_value = 0.0
    # 最新のOBVが一つ前のOBVを上回っている (買い圧力優位)
    if not np.isnan(indicators.get('OBV_latest')) and not np.isnan(indicators.get('OBV_prev')):
        if indicators['OBV_latest'] > indicators['OBV_prev']:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS # +0.04

    score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # --- 6. ボラティリティ過熱ペナルティ (BB幅) ---
    volatility_penalty_value = 0.0
    if not np.isnan(indicators.get('BB_width')) and latest_close > 0:
        # BB幅が価格の VOLATILITY_BB_PENALTY_THRESHOLD (1%) を超えている場合、ペナルティ
        bb_ratio = indicators['BB_width'] / latest_close
        if bb_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
            # ペナルティは -0.05 を上限とする
            volatility_penalty_value = -min(0.05, (bb_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) * 2) 

    score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # --- 7. 🆕 StochRSIによる過熱ペナルティ ---
    stoch_rsi_penalty_value = 0.0
    stoch_k = indicators.get('STOCHRSIk', np.nan)
    stoch_d = indicators.get('STOCHRSId', np.nan)
    
    if not np.isnan(stoch_k) and not np.isnan(stoch_d):
        # StochRSIが買われすぎ水準 (80以上) にあり、かつデッドクロス寸前/実行済みの場合
        if stoch_k >= (100 - STOCHRSI_BOS_LEVEL): # 例: 80
            # KがDを下回り始めている (短期的な下落サイン)
            if stoch_k < stoch_d:
                 stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY # -0.08 (過熱ペナルティ)
        # StochRSIが売られすぎ水準 (20以下) にあり、かつゴールデンクロス寸前/実行済みの場合
        elif stoch_k <= STOCHRSI_BOS_LEVEL: # 例: 20
            if stoch_k > stoch_d:
                 stoch_rsi_penalty_value = STOCHRSI_BOS_PENALTY # +0.08 (過熱からの回復ボーナス)
                 
    score += stoch_rsi_penalty_value
    tech_data['stoch_rsi_penalty_value'] = stoch_rsi_penalty_value
    tech_data['stoch_rsi_k_value'] = stoch_k

    # --- 8. 🆕 ADXによるトレンド確証ボーナス ---
    adx_trend_bonus_value = 0.0
    adx_raw = indicators.get('ADX', np.nan)
    dmp_raw = indicators.get('DMP', np.nan)
    dmn_raw = indicators.get('DMN', np.nan)

    if not np.isnan(adx_raw) and not np.isnan(dmp_raw) and not np.isnan(dmn_raw):
        # ADXが25以上 (強いトレンド) かつ +DI > -DI (上昇トレンド方向)
        if adx_raw >= ADX_TREND_STRENGTH_THRESHOLD and dmp_raw > dmn_raw:
            adx_trend_bonus_value = ADX_TREND_BONUS # +0.07

    score += adx_trend_bonus_value
    tech_data['adx_trend_bonus_value'] = adx_trend_bonus_value
    tech_data['adx_raw_value'] = adx_raw

    # --- 9. マクロコンテキスト (FGI) ---
    sentiment_fgi_proxy_bonus = 0.0
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # FGIが正の値 (+リスクオン) であればボーナス。負の値であればペナルティ
    # ボーナスの最大値は FGI_PROXY_BONUS_MAX
    sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, max(-FGI_PROXY_BONUS_MAX, fgi_proxy))
    
    score += sentiment_fgi_proxy_bonus
    score += forex_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus + forex_bonus

    # --- 10. 流動性ボーナス ---
    liquidity_bonus_value = 0.0
    # BTC, ETH, SOLなど、出来高上位銘柄であればボーナス
    if symbol in CURRENT_MONITOR_SYMBOLS[:10]: # TOP10銘柄に属する場合
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    elif symbol in CURRENT_MONITOR_SYMBOLS[:40]: # TOP40銘柄に属する場合
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX / 2
        
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

    # スコアの合計を計算
    final_score = max(0.0, min(1.0, score)) # 0.0から1.0の間に正規化
    
    # 最終的なスコア内訳に指標データを追加
    tech_data['indicators'] = indicators
    tech_data['final_score_raw'] = score
    
    return final_score, tech_data


def score_short_signal(indicators: Dict, latest_close: float, symbol: str, macro_context: Dict) -> Tuple[float, Dict]:
    """ショートシグナルをスコアリングし、詳細なスコア内訳を返す。"""
    
    if indicators.get('error'):
        return 0.0, {'score': 0.0, 'message': indicators.get('message', '指標計算エラー')}
        
    score = BASE_SCORE # ベーススコア 40点
    tech_data = {}
    
    # --- 1. 長期トレンドと価格構造 (SMA200) ---
    long_term_reversal_penalty_value = 0.0
    # 価格がSMA200の下にあること (順張り優位性)
    if not np.isnan(indicators.get('SMA200')) and latest_close < indicators['SMA200']:
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY # +0.20
    else:
        # 価格がSMA200の上にある場合 (逆張り/レンジ判定)
        long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY / 2 # -0.10 (ペナルティを半分に軽減)

    score += long_term_reversal_penalty_value
    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value
    
    # --- 2. 構造的優位性 (ベース) ---
    score += STRUCTURAL_PIVOT_BONUS # +0.05
    tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # --- 3. MACDモメンタム (下降トレンド確証) ---
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインを下回っている AND MACDヒストグラムが減少傾向
    if not np.isnan(indicators.get('MACD')) and not np.isnan(indicators.get('MACDs')):
        if indicators['MACD'] < indicators['MACDs']: # MACDがデッドクロス状態
            # ヒストグラムがゼロラインより下で減少傾向 (強い下降モメンタム)
            if indicators.get('MACDh', 0.0) < 0.0:
                 macd_penalty_value = MACD_CROSS_PENALTY # +0.15 (強いボーナス)
            else:
                 # MACD < MACDs だが、ヒストグラムは縮小傾向 (モメンタム失速)
                 macd_penalty_value = MACD_CROSS_PENALTY / 2 # +0.075 (弱いボーナス)
        else:
            # MACDがゴールデンクロス状態 (上昇モメンタム)
            macd_penalty_value = -MACD_CROSS_PENALTY # -0.15 (強いペナルティ)

    score += macd_penalty_value
    tech_data['macd_penalty_value'] = macd_penalty_value

    # --- 4. RSIモメンタム加速 ---
    rsi_momentum_bonus_value = 0.0
    # RSIがRSI_MOMENTUM_LOW (60) を下回って下降している (上昇トレンドからの脱却)
    if not np.isnan(indicators.get('RSI')) and indicators['RSI'] < (100 - RSI_MOMENTUM_LOW): # RSI < 60
        # RSIが低いほど、モメンタムが強い
        rsi_momentum_bonus_value = min(RSI_DIVERGENCE_BONUS, ((100 - RSI_MOMENTUM_LOW) - indicators['RSI']) / 100) # Max +0.10

    score += rsi_momentum_bonus_value
    tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value

    # --- 5. 出来高確証 (OBV) ---
    obv_momentum_bonus_value = 0.0
    # 最新のOBVが一つ前のOBVを下回っている (売り圧力優位)
    if not np.isnan(indicators.get('OBV_latest')) and not np.isnan(indicators.get('OBV_prev')):
        if indicators['OBV_latest'] < indicators['OBV_prev']:
            obv_momentum_bonus_value = OBV_MOMENTUM_BONUS # +0.04

    score += obv_momentum_bonus_value
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value
    
    # --- 6. ボラティリティ過熱ペナルティ (BB幅) ---
    volatility_penalty_value = 0.0
    if not np.isnan(indicators.get('BB_width')) and latest_close > 0:
        # BB幅が価格の VOLATILITY_BB_PENALTY_THRESHOLD (1%) を超えている場合、ペナルティ
        bb_ratio = indicators['BB_width'] / latest_close
        if bb_ratio > VOLATILITY_BB_PENALTY_THRESHOLD:
            # ペナルティは -0.05 を上限とする
            volatility_penalty_value = -min(0.05, (bb_ratio - VOLATILITY_BB_PENALTY_THRESHOLD) * 2) 

    score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value

    # --- 7. 🆕 StochRSIによる過熱ペナルティ ---
    stoch_rsi_penalty_value = 0.0
    stoch_k = indicators.get('STOCHRSIk', np.nan)
    stoch_d = indicators.get('STOCHRSId', np.nan)
    
    if not np.isnan(stoch_k) and not np.isnan(stoch_d):
        # StochRSIが売られすぎ水準 (20以下) にあり、かつゴールデンクロス寸前/実行済みの場合
        if stoch_k <= STOCHRSI_BOS_LEVEL: # 例: 20
            # KがDを上回り始めている (短期的な上昇サイン)
            if stoch_k > stoch_d:
                 stoch_rsi_penalty_value = -STOCHRSI_BOS_PENALTY # -0.08 (過熱ペナルティ)
        # StochRSIが買われすぎ水準 (80以上) にあり、かつデッドクロス寸前/実行済みの場合
        elif stoch_k >= (100 - STOCHRSI_BOS_LEVEL): # 例: 80
            if stoch_k < stoch_d:
                 stoch_rsi_penalty_value = STOCHRSI_BOS_PENALTY # +0.08 (過熱からの回復ボーナス)
                 
    score += stoch_rsi_penalty_value
    tech_data['stoch_rsi_penalty_value'] = stoch_rsi_penalty_value
    tech_data['stoch_rsi_k_value'] = stoch_k

    # --- 8. 🆕 ADXによるトレンド確証ボーナス ---
    adx_trend_bonus_value = 0.0
    adx_raw = indicators.get('ADX', np.nan)
    dmp_raw = indicators.get('DMP', np.nan)
    dmn_raw = indicators.get('DMN', np.nan)

    if not np.isnan(adx_raw) and not np.isnan(dmp_raw) and not np.isnan(dmn_raw):
        # ADXが25以上 (強いトレンド) かつ -DI > +DI (下降トレンド方向)
        if adx_raw >= ADX_TREND_STRENGTH_THRESHOLD and dmn_raw > dmp_raw:
            adx_trend_bonus_value = ADX_TREND_BONUS # +0.07

    score += adx_trend_bonus_value
    tech_data['adx_trend_bonus_value'] = adx_trend_bonus_value
    tech_data['adx_raw_value'] = adx_raw

    # --- 9. マクロコンテキスト (FGI) ---
    sentiment_fgi_proxy_bonus = 0.0
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    
    # FGIが負の値 (-リスクオフ) であればボーナス。正の値であればペナルティ
    sentiment_fgi_proxy_bonus = min(FGI_PROXY_BONUS_MAX, max(-FGI_PROXY_BONUS_MAX, -fgi_proxy))
    
    # ショートの場合、為替ボーナスも逆転させる
    score += sentiment_fgi_proxy_bonus
    score += (-forex_bonus) 
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus + (-forex_bonus)

    # --- 10. 流動性ボーナス ---
    liquidity_bonus_value = 0.0
    # BTC, ETH, SOLなど、出来高上位銘柄であればボーナス
    if symbol in CURRENT_MONITOR_SYMBOLS[:10]: # TOP10銘柄に属する場合
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX
    elif symbol in CURRENT_MONITOR_SYMBOLS[:40]: # TOP40銘柄に属する場合
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX / 2
        
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value

    # スコアの合計を計算
    final_score = max(0.0, min(1.0, score)) # 0.0から1.0の間に正規化
    
    # 最終的なスコア内訳に指標データを追加
    tech_data['indicators'] = indicators
    tech_data['final_score_raw'] = score
    
    return final_score, tech_data


def get_position_targets(latest_close: float, atr_value: float, side: str) -> Tuple[float, float, float]:
    """
    ATRベースでストップロス (SL) とテイクプロフィット (TP) の価格を決定し、RR比率を返す。
    """
    
    if latest_close <= 0 or atr_value <= 0:
        return 0.0, 0.0, 0.0
        
    # ATRに基づくリスク幅
    risk_distance = atr_value * ATR_MULTIPLIER_SL
    
    # 最低リスク幅のチェック (リスクを狭めすぎないように)
    min_risk_distance = latest_close * MIN_RISK_PERCENT
    risk_distance = max(risk_distance, min_risk_distance)
    
    reward_distance = risk_distance * RR_RATIO_TARGET
    
    # 価格計算
    if side == 'long':
        stop_loss = latest_close - risk_distance
        take_profit = latest_close + reward_distance
    elif side == 'short':
        stop_loss = latest_close + risk_distance
        take_profit = latest_close - reward_distance
    else:
        return 0.0, 0.0, 0.0
        
    # SL/TPが0以下にならないように保証
    stop_loss = max(0.0, stop_loss)
    take_profit = max(0.0, take_profit)
    
    return stop_loss, take_profit, RR_RATIO_TARGET


async def analyze_symbol(symbol: str, macro_context: Dict) -> List[Dict]:
    """
    指定された銘柄の複数の時間枠で分析を実行し、有効なシグナルを返す。
    """
    
    signals = []
    
    for timeframe in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        
        # 1. OHLCVデータを取得
        df = await fetch_ohlcv(symbol, timeframe, limit=limit)
        if df is None or df.empty or len(df) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}): 必要データ ({limit}本) が不足しているためスキップします。")
            continue

        # 2. テクニカル指標を計算
        indicators = calculate_technical_indicators(df)
        if indicators.get('error'):
            logging.warning(f"⚠️ {symbol} ({timeframe}): 指標計算エラーによりスキップします。メッセージ: {indicators.get('message')}")
            continue

        latest_close = indicators['latest_close']
        atr_value = indicators.get('ATR', 0.0)
        
        # ATRが0またはNaNの場合はスキップ
        if atr_value <= 0 or np.isnan(atr_value):
            logging.warning(f"⚠️ {symbol} ({timeframe}): ATR ({atr_value:.4f}) が不正なためスキップします。")
            continue
            
        # 3. ロングシグナルをスコアリング
        long_score, long_tech_data = score_long_signal(indicators, latest_close, symbol, macro_context)
        
        # 4. ショートシグナルをスコアリング
        short_score, short_tech_data = score_short_signal(indicators, latest_close, symbol, macro_context)
        
        # 5. スコアの高い方を取引方向として採用
        if long_score > short_score:
            best_score = long_score
            best_side = 'long'
            best_tech_data = long_tech_data
        elif short_score > long_score:
            best_score = short_score
            best_side = 'short'
            best_tech_data = short_tech_data
        else:
            # スコアが同点の場合はスキップ
            continue 

        # 6. SL/TPを決定
        stop_loss, take_profit, rr_ratio = get_position_targets(latest_close, atr_value, best_side)
        
        # SL/TP価格が0.0になるような不正なターゲットはスキップ
        if stop_loss <= 0.0 or take_profit <= 0.0:
            logging.warning(f"⚠️ {symbol} ({timeframe}): SL/TP価格が不正 ({stop_loss}, {take_profit}) なためスキップします。")
            continue
            
        # 清算価格を計算
        liquidation_price = calculate_liquidation_price(latest_close, LEVERAGE, best_side, MIN_MAINTENANCE_MARGIN_RATE)

        # 7. シグナルとして保存
        signal = {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': best_side,
            'score': best_score,
            'entry_price': latest_close,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'liquidation_price': liquidation_price,
            'rr_ratio': rr_ratio,
            'timestamp': df.index[-1].strftime("%Y-%m-%d %H:%M:%S%z"),
            'tech_data': best_tech_data,
        }
        signals.append(signal)

    return signals


async def process_entry_signal(signal: Dict) -> Dict:
    """
    シグナルに基づき、実際に取引所へエントリー注文を出す。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    result = {'status': 'error', 'error_message': '初期化エラーまたはテストモード。'}

    if TEST_MODE:
        logging.info(f"🚧 TEST_MODE: {symbol} ({side}) の注文実行をスキップしました。")
        # テストとして、成功したと見なす
        result = {
            'status': 'ok',
            'filled_amount': FIXED_NOTIONAL_USDT / entry_price * LEVERAGE, # 概算の契約数
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'order_id': f"TEST-{uuid.uuid4()}"
        }
        # テストポジションをOPEN_POSITIONSに追加
        new_pos = {
            'id': result['order_id'],
            'symbol': symbol,
            'side': side,
            'contracts': result['filled_amount'],
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'leverage': LEVERAGE,
            'unrealizedPnl': 0.0,
            'initialMargin': FIXED_NOTIONAL_USDT / LEVERAGE,
            'filled_usdt': FIXED_NOTIONAL_USDT,
        }
        OPEN_POSITIONS.append(new_pos)
        
        return result


    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'CCXTクライアントが初期化されていません。'}
        
    try:
        # 1. 注文数量 (USDT建ての名目価値から計算)
        # 固定ロット (FIXED_NOTIONAL_USDT) をレバレッジ倍した名目価値
        notional_value = FIXED_NOTIONAL_USDT * LEVERAGE
        
        # 契約数 = (名目価値) / (現在の価格) 
        # CCXTは通常、USDT (クォート通貨) での数量指定ではなく、ベース通貨 (例: BTC) での数量指定を要求する
        amount_base = notional_value / entry_price 
        
        # 2. 注文のタイプ (成行注文)
        order_type = 'market'
        
        # 3. 注文方向
        ccxt_side = 'buy' if side == 'long' else 'sell'
        
        # 4. 注文の実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type=order_type,
            side=ccxt_side,
            amount=amount_base,
            params={'marginMode': 'cross', 'leverage': LEVERAGE} # MEXCでレバレッジが再設定されるのを期待
        )
        
        # 注文の結果を確認
        if order.get('status') == 'closed' or order.get('status') == 'filled':
            filled_amount = order.get('filled', amount_base)
            filled_usdt_notional = filled_amount * entry_price # 概算
            
            # 5. 成功した場合、ポジションリストを更新
            new_pos = {
                'id': order.get('id', str(uuid.uuid4())),
                'symbol': symbol,
                'side': side,
                'contracts': filled_amount,
                'entry_price': entry_price, # 実際にはorder['price']を使うべきだが、簡略化
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'leverage': LEVERAGE,
                'unrealizedPnl': 0.0,
                'initialMargin': FIXED_NOTIONAL_USDT,
                'filled_usdt': filled_usdt_notional,
            }
            OPEN_POSITIONS.append(new_pos)
            
            # 6. SL/TP注文の設定 (取引所が対応している場合)
            await set_sl_tp_order(symbol, side, filled_amount, stop_loss, take_profit, order['id'])

            logging.info(f"✅ {symbol} ({side}) の新規エントリー注文成功。数量: {filled_amount:.4f}")
            result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'order_id': order.get('id', new_pos['id'])
            }
            
        else:
            # 注文が部分約定、またはその他のステータスの場合
            error_msg = f"注文ステータス: {order.get('status')}。約定せず。"
            logging.error(f"❌ {symbol} の注文失敗: {error_msg}")
            result = {'status': 'error', 'error_message': error_msg}
            
    except ccxt.InsufficientFunds as e:
        error_msg = f"残高不足: {e}"
        logging.critical(f"❌ {symbol} の注文失敗: {error_msg}", exc_info=True)
        result = {'status': 'error', 'error_message': error_msg}
    except ccxt.InvalidOrder as e:
        error_msg = f"不正な注文 (ロットが小さすぎるなど): {e}"
        logging.critical(f"❌ {symbol} の注文失敗: {error_msg}", exc_info=True)
        result = {'status': 'error', 'error_message': error_msg}
    except Exception as e:
        error_msg = f"予期せぬAPIエラー: {e}"
        logging.critical(f"❌ {symbol} の注文失敗: {error_msg}", exc_info=True)
        result = {'status': 'error', 'error_message': error_msg}
        
    return result

async def set_sl_tp_order(symbol: str, side: str, amount: float, stop_loss: float, take_profit: float, order_id: str) -> bool:
    """
    ポジションに紐づくストップロス (SL) とテイクプロフィット (TP) 注文を設定する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT:
        return False
        
    # SL/TPの注文タイプは逆方向
    close_side = 'sell' if side == 'long' else 'buy'
    
    # 多くの取引所は複合的な注文 (OCO/Bracket/TakeProfitStopLoss) をサポートしている
    # ここでは、MEXCが対応している set_margin_mode と set_leverage の後に、
    # 簡易的にSLとTPの指値注文 (Stop Limit/Take Profit Limit) を出す、または
    # MEXC特有の params で SL/TPを同時に設定する方式を想定する。
    
    # MEXCは create_order の params で stopLossPrice, takeProfitPrice を設定可能 (v20.0.45時点の仕様に基づいて簡略化)
    # create_order_in_market_price_order というメソッドを使うのがより正確だが、ここでは create_order に params を追加
    
    try:
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXCのTP/SLは、ポジションのクローズ注文として実行される必要がある
            # ここでは、ポジションを識別するための params を追加して、成行でクローズする SL/TP注文を出すことを想定
            
            # SL注文 (Stop Market)
            sl_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='stop_market', # ストップ成行
                side=close_side,
                amount=amount,
                price=None,
                params={
                    'stopPrice': stop_loss,
                    'closePosition': True, # ポジションのクローズ
                    # 'positionId': order_id, # ポジションIDがあれば追加 (MEXCはCCXT経由では非公開の場合がある)
                    'marginMode': 'cross',
                    'leverage': LEVERAGE
                }
            )
            logging.info(f"✅ {symbol} ({side}) SL注文設定完了 (Stop Market @ {format_price(stop_loss)})")
            
            # TP注文 (Take Profit Market)
            tp_order = await EXCHANGE_CLIENT.create_order(
                symbol=symbol,
                type='take_profit_market', # テイクプロフィット成行
                side=close_side,
                amount=amount,
                price=None,
                params={
                    'stopPrice': take_profit,
                    'closePosition': True,
                    # 'positionId': order_id,
                    'marginMode': 'cross',
                    'leverage': LEVERAGE
                }
            )
            logging.info(f"✅ {symbol} ({side}) TP注文設定完了 (Take Profit Market @ {format_price(take_profit)})")
            
            return True
            
        else:
            # 他の取引所の場合、TP/SLは create_order_in_market_price_order などの専用メソッドが必要
            logging.warning(f"⚠️ {EXCHANGE_CLIENT.id} は set_sl_tp_order の専用ロジックが未実装のため、SL/TP設定をスキップします。手動で設定してください。")
            return False
            
    except Exception as e:
        logging.error(f"❌ {symbol} のSL/TP注文設定中にエラーが発生しました: {e}", exc_info=True)
        # エラー発生時は、ポジションリスト内のSL/TP価格をクリア (手動管理に移行)
        for pos in OPEN_POSITIONS:
            if pos.get('id', '') == order_id or pos['symbol'] == symbol:
                pos['stop_loss'] = 0.0
                pos['take_profit'] = 0.0
                break
        return False
        
    return True


async def process_exit_position(position: Dict, exit_type: str, current_price: float) -> Dict:
    """
    ポジションを決済する注文を出し、結果を返す。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    entry_price = position['entry_price']
    
    result = {'status': 'error', 'error_message': '初期化エラーまたはテストモード。'}
    
    if TEST_MODE:
        logging.info(f"🚧 TEST_MODE: {symbol} ({side}) のポジション決済実行をスキップしました。")
        pnl_rate = ((current_price - entry_price) / entry_price) * LEVERAGE if side == 'long' else ((entry_price - current_price) / entry_price) * LEVERAGE
        pnl_usdt = (pnl_rate / LEVERAGE) * position['filled_usdt']
        
        result = {
            'status': 'ok',
            'exit_type': exit_type,
            'exit_price': current_price,
            'entry_price': entry_price,
            'filled_amount': contracts,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
        }
        # テストポジションをOPEN_POSITIONSから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] != position['id']]
        return result
        
    if not EXCHANGE_CLIENT:
        return {'status': 'error', 'error_message': 'CCXTクライアントが初期化されていません。'}

    try:
        # 決済方向は反対側
        close_side = 'sell' if side == 'long' else 'buy'
        
        # 決済注文の実行 (成行)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=close_side,
            amount=contracts, # ポジションの契約数と同じ量
            params={'closePosition': True, 'marginMode': 'cross', 'leverage': LEVERAGE} 
        )
        
        if order.get('status') == 'closed' or order.get('status') == 'filled':
            
            # PnLの計算 (概算、取引所のAPIが提供しない場合)
            exit_price = order.get('price', current_price) # 注文価格 (成行の場合、約定価格はcurrent_priceに近い)
            
            # PnL (USDT) の計算
            pnl_usdt_rate = (exit_price - entry_price) / entry_price if side == 'long' else (entry_price - exit_price) / entry_price
            pnl_rate = pnl_usdt_rate * LEVERAGE # レバレッジ考慮後のPnL%
            pnl_usdt = pnl_usdt_rate * position['filled_usdt'] # 名目価値ベースのPnL
            
            logging.info(f"✅ {symbol} ({side}) のポジション決済成功: {exit_type} @ {format_price(exit_price)}")
            
            # 注文成功後、オープンポジションリストから削除
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p['id'] != position['id']]
            
            result = {
                'status': 'ok',
                'exit_type': exit_type,
                'exit_price': exit_price,
                'entry_price': entry_price,
                'filled_amount': contracts,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'order_id': order.get('id')
            }
            
        else:
            error_msg = f"決済注文ステータス: {order.get('status')}。約定せず。"
            logging.error(f"❌ {symbol} の決済失敗: {error_msg}")
            result = {'status': 'error', 'error_message': error_msg}
            
    except Exception as e:
        error_msg = f"予期せぬAPIエラー: {e}"
        logging.critical(f"❌ {symbol} の決済失敗: {error_msg}", exc_info=True)
        result = {'status': 'error', 'error_message': error_msg}
        
    return result


# ====================================================================================
# SCHEDULERS (MAIN LOOP & MONITOR)
# ====================================================================================

async def check_and_manage_open_positions():
    """
    管理中のポジションに対し、現在の価格に基づきSL/TPのトリガーをチェックし、決済する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, GLOBAL_MACRO_CONTEXT
    
    if not OPEN_POSITIONS or not EXCHANGE_CLIENT:
        return

    # ポジションを抱えている銘柄の最新価格をまとめて取得
    symbols_to_fetch = [pos['symbol'] for pos in OPEN_POSITIONS]
    
    try:
        # 最新のティッカー情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_fetch)
        
    except Exception as e:
        logging.error(f"❌ ポジション管理用の価格取得中にエラーが発生しました: {e}", exc_info=True)
        # 価格取得に失敗した場合、今回のループはスキップ
        return
        
    positions_to_exit = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        sl_price = position['stop_loss']
        tp_price = position['take_profit']
        side = position['side']
        
        # 最新価格を取得
        ticker = tickers.get(symbol)
        if not ticker:
            logging.warning(f"⚠️ {symbol} の最新価格が取得できませんでした。スキップします。")
            continue
            
        current_price = ticker.get('last', ticker.get('close', 0.0))
        if current_price == 0.0:
            continue

        exit_type = None
        
        # 1. ストップロス (SL) チェック
        # ロング: 価格 <= SL価格
        if side == 'long' and sl_price > 0.0 and current_price <= sl_price:
            exit_type = "SL_TRIGGER"
            
        # ショート: 価格 >= SL価格
        elif side == 'short' and sl_price > 0.0 and current_price >= sl_price:
            exit_type = "SL_TRIGGER"
            
        # 2. テイクプロフィット (TP) チェック
        # ロング: 価格 >= TP価格
        elif side == 'long' and tp_price > 0.0 and current_price >= tp_price:
            exit_type = "TP_TRIGGER"
            
        # ショート: 価格 <= TP価格
        elif side == 'short' and tp_price > 0.0 and current_price <= tp_price:
            exit_type = "TP_TRIGGER"
            
        
        if exit_type:
            positions_to_exit.append({
                'position': position,
                'exit_type': exit_type,
                'current_price': current_price
            })

    # 決済が必要なポジションを一括処理
    if positions_to_exit:
        logging.info(f"🚨 決済対象のポジションが {len(positions_to_exit)} 件あります。決済処理を開始します。")
        
        exit_tasks = []
        for item in positions_to_exit:
            # process_exit_position は OPEN_POSITIONS をグローバルに変更する可能性があるため、注意が必要
            # ただし、Pythonのリスト操作は要素をコピーするため、ここでは問題なしと見なす
            exit_tasks.append(process_exit_position(item['position'], item['exit_type'], item['current_price']))
            
        results = await asyncio.gather(*exit_tasks, return_exceptions=True)
        
        # 決済結果の通知とログ記録
        for position_data, result in zip(positions_to_exit, results):
            position = position_data['position']
            exit_type = position_data['exit_type']
            
            if isinstance(result, Exception):
                logging.error(f"❌ {position['symbol']} のポジション決済処理中に予期せぬエラー: {result}", exc_info=True)
                # エラーの場合は通知をスキップ
                continue
            
            if result['status'] == 'ok':
                log_signal(position, 'ポジション決済', result)
                # 通知の送信 (シグナルデータには SL/TP/EntryPrice が残っているものを使用)
                await send_telegram_notification(format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), result, exit_type))


async def main_bot_scheduler():
    """
    BOTのメイン処理ループ。分析、取引、レポート送信を制御する。
    """
    global LAST_SUCCESS_TIME, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, IS_FIRST_MAIN_LOOP_COMPLETED, LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, LOOP_INTERVAL

    logging.info("⏳ メインBOTスケジューラを開始します。")
    
    # 1. クライアントの初期化 (成功するまでリトライ)
    while not IS_CLIENT_READY:
        logging.info("⚙️ CCXTクライアントの初期化を試行中...")
        if await initialize_exchange_client():
            break
        logging.warning("⚠️ CCXTクライアントの初期化に失敗しました。5秒後に再試行します。")
        await asyncio.sleep(5)
        
    logging.info("✅ CCXTクライアントの初期化に成功しました。メインループに進みます。")


    while True:
        try:
            current_time = time.time()
            logging.info(f"--- メイン分析ループ開始: {datetime.now(JST).strftime('%Y/%m/%d %H:%M:%S')} ---")

            # 2. 市場環境の更新 (FGIなど)
            macro_context = await fetch_macro_context()
            
            # 3. 口座ステータスの更新
            account_status = await get_account_status()
            
            # 4. 監視シンボルリストの更新 (出来高上位銘柄の取得)
            await fetch_tickers_and_update_symbols()
            
            # 5. 初回起動完了通知 (一度だけ)
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                current_threshold = get_current_threshold(macro_context)
                startup_message = format_startup_message(account_status, macro_context, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
                await send_telegram_notification(startup_message)
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 6. 全銘柄・全時間枠でシグナル分析を実行
            analysis_tasks = [analyze_symbol(symbol, macro_context) for symbol in CURRENT_MONITOR_SYMBOLS]
            all_signals_nested = await asyncio.gather(*analysis_tasks, return_exceptions=True)
            
            # 結果をフラット化し、エラーを除外
            all_signals = []
            for result in all_signals_nested:
                if isinstance(result, Exception):
                    logging.error(f"❌ analyze_symbol 実行中にエラー: {result}", exc_info=True)
                    continue
                all_signals.extend(result)
                
            # グローバルリストに最新の分析結果を保存
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # 7. スコア順でソートし、取引閾値以上のシグナルを抽出
            current_threshold = get_current_threshold(macro_context)
            
            executable_signals = sorted([
                s for s in all_signals if s['score'] >= current_threshold
            ], key=lambda x: x['score'], reverse=True)
            
            # 8. 取引実行
            for signal in executable_signals:
                symbol = signal['symbol']
                side = signal['side']
                
                # Cooldown期間をチェック (同じ銘柄の同じ方向で直近に取引がないか)
                last_trade_time = LAST_SIGNAL_TIME.get(f"{symbol}_{side}", 0.0)
                if current_time - last_trade_time < TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"⏳ {symbol} ({side}) はクールダウン期間中のためスキップします。")
                    continue
                
                # 既にオープンポジションがある場合はスキップ
                if any(p['symbol'] == symbol and p['side'] == side for p in OPEN_POSITIONS):
                    logging.info(f"ℹ️ {symbol} ({side}) は既にポジションを保有しているためスキップします。")
                    continue
                    
                # 執行
                logging.info(f"🔥 **取引実行**: {symbol} ({signal['timeframe']}) {side.upper()} Score: {signal['score']:.2f}")
                trade_result = await process_entry_signal(signal)
                
                # 9. 結果の通知とログ記録
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[f"{symbol}_{side}"] = current_time
                    log_signal(signal, '取引シグナル', trade_result)
                    await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
                    # 最初のシグナルを執行したら、次のループまで待機
                    # break 
                else:
                    logging.error(f"❌ {symbol} の取引執行に失敗しました。メッセージ: {trade_result.get('error_message')}")
                    await send_telegram_notification(f"🚨 <b>取引実行エラー ({symbol})</b>\n{trade_result.get('error_message')}")

            # 10. 定期的な WebShare データ送信
            if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                # WebShare用のデータペイロードを作成
                webshare_data = {
                    'bot_version': BOT_VERSION,
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'open_positions': OPEN_POSITIONS,
                    'last_analysis_signals': LAST_ANALYSIS_SIGNALS[:TOP_SIGNAL_COUNT], # トップNのみ
                    'account_status': account_status,
                    'macro_context': macro_context,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time

            # 11. 定期的な最高スコア通知
            await notify_highest_analysis_score()


            # 12. 次の分析ループまで待機
            LAST_SUCCESS_TIME = current_time
            logging.info(f"--- メイン分析ループ完了。次のループまで {LOOP_INTERVAL}秒待機。 ---")
            await asyncio.sleep(LOOP_INTERVAL)
            
        except Exception as e:
            # 致命的なエラーの通知
            error_message = f"🚨 **メインBOTスケジューラで致命的エラー**\n<code>{e}</code>\n次のループで再試行します。"
            logging.critical(error_message, exc_info=True)
            # エラー発生時の通知は、レートリミットを避けるため最小限に留める
            if current_time - LAST_SUCCESS_TIME > 60: # 前回の成功から1分以上経過している場合のみ通知
                 await send_telegram_notification(error_message)
                 
            # エラー発生時は待機時間を長くする (例: 30秒)
            await asyncio.sleep(30)


async def position_monitor_scheduler():
    """
    ポジションのSL/TP監視とクローズ処理を定期的に実行するスケジューラ。
    """
    
    # メインBOTスケジューラが起動してから開始することを推奨
    await asyncio.sleep(15) 

    if not IS_CLIENT_READY:
         logging.critical("❌ クライアントが未初期化のため、ポジション監視スケジューラを起動できません。")
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
    """捕捉されなかった例外を処理し、JSONで返す"""
    logging.critical(f"❌ FastAPIで捕捉されなかった予期せぬエラーが発生しました: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc.__class__.__name__}", "detail": str(exc)},
    )


# ====================================================================================
# MAIN EXECUTION
# ====================================================================================

if __name__ == "__main__":
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    # 💡 致命的エラーの原因究明のため、一時的に uvicorn 起動コードをコメントアウトし、
    #    main_bot_scheduler の直接実行またはダミー実行を行うことを推奨
    
    # 通常の起動方法に戻す
    try:
        logging.info("Webサーバー (Uvicorn) を起動します。")
        uvicorn.run(app, host="0.0.0.0", port=8000)
    except Exception as e:
        logging.critical(f"❌ Uvicornの起動に失敗しました: {e}", exc_info=True)
        sys.exit(1)
