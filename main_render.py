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
                
                # totalEquity が 0でない、かつ ccxtの total_usdt_balance が 0 の場合はフォールバック
                if total_usdt_balance_fallback > 0.0 and total_usdt_balance == 0.0:
                     total_usdt_balance = total_usdt_balance_fallback
                     logging.info(f"ℹ️ MEXC残高: CCXTのtotalが0のため、info.data.totalEquity ({total_usdt_balance:.2f} USDT) にフォールバックしました。")
                elif total_usdt_balance == 0.0 and total_usdt_balance_fallback > 0.0:
                    # totalが0でもinfoから取得できた場合は使用する
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.info(f"ℹ️ MEXC残高: CCXTのtotalが取得できないため、info.data.totalEquity ({total_usdt_balance:.2f} USDT) を使用しました。")
                
        if total_usdt_balance == 0.0:
             logging.warning("⚠️ 総資産 (Equity) が0.0 USDTと報告されました。APIキーの権限設定を確認してください。")

        ACCOUNT_EQUITY_USDT = total_usdt_balance
        
        # ポジション情報の取得 (Open Positions)
        # ccxtのfetch_positionsは統一されていないため、カスタム実装が必要な場合がある
        open_positions = []
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions = await EXCHANGE_CLIENT.fetch_positions(params={'defaultType': 'swap'})
            
            for pos in positions:
                # ポジションサイズが0でないもののみをフィルタリング
                # MEXCの場合、contractsが数量
                contracts_raw = pos.get('contracts', 0.0) 
                if contracts_raw != 0.0:
                    
                    symbol = pos.get('symbol')
                    entry_price = float(pos.get('entryPrice', 0.0))
                    side = 'long' if float(contracts_raw) > 0 else 'short'
                    
                    # MEXCのポジションはシンボル名が :USDT形式 (例: BTC/USDT:USDT) の場合があるため、統一
                    if symbol and symbol.endswith(':USDT'):
                        symbol = symbol.replace(':USDT', '')

                    # 💡 注意: CCXTのポジションオブジェクトには通常 SL/TP 情報が含まれていないため、
                    # Botが管理している OPEN_POSITIONS リストと照合するロジックが必要だが、
                    # この関数ではCCXTからの生のデータのみを取得し、管理ロジックは main_bot_scheduler に任せる。

                    # ロットサイズ (名目価値) の計算
                    filled_usdt = abs(float(contracts_raw)) * entry_price if entry_price > 0 else 0.0
                    
                    open_positions.append({
                        'symbol': symbol,
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': abs(float(contracts_raw)),
                        'filled_usdt': filled_usdt,
                        'info': pos, # CCXTの生データ
                    })
        
        # グローバル変数にCCXTから取得した生のポジション情報を格納 (Bot管理SL/TPは別ロジックで結合)
        # ここでは純粋なCCXTのポジションリストを返し、Bot管理ポジションはメインループで更新
        
        logging.info(f"✅ 口座ステータス取得: 総資産: {total_usdt_balance:.2f} USDT, CCXTポジション数: {len(open_positions)}")
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'open_positions': open_positions,
            'error': False
        }
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得中にエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

async def fetch_top_volume_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所から出来高の高いシンボルを取得し、DEFAULT_SYMBOLSと結合して返す。
    """
    global EXCHANGE_CLIENT, SKIP_MARKET_UPDATE
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY or SKIP_MARKET_UPDATE:
        logging.warning("⚠️ CCXTクライアントが未準備か、市場アップデートがスキップされています。デフォルトシンボルを返します。")
        return DEFAULT_SYMBOLS.copy()

    try:
        # 市場情報の取得 (CCXTのTickerを使用)
        # MEXCの場合、fetch_tickers(params={'defaultType': 'swap'}) を使用
        if EXCHANGE_CLIENT.id == 'mexc':
            tickers = await EXCHANGE_CLIENT.fetch_tickers(params={'defaultType': 'swap'})
        else:
            tickers = await EXCHANGE_CLIENT.fetch_tickers()

        if not isinstance(tickers, dict) or not tickers.keys(): 
            logging.warning("⚠️ Tickersデータが空または無効です。デフォルトシンボルを使用します。")
            return DEFAULT_SYMBOLS.copy()

        # USDT建ての先物市場のみをフィルタリングし、24hの出来高(quoteVolume)でソート
        usdt_future_tickers = []
        for symbol, ticker in tickers.items():
            if ticker is None: # NoneType object has no attribute 'keys' 対策
                continue
            
            # 市場情報から、USDT建ての先物・スワップ市場であることを確認
            market = EXCHANGE_CLIENT.markets.get(symbol)
            if market and market.get('quote') == 'USDT' and market.get('type') in ['future', 'swap']:
                # 出来高 (quoteVolume, USDTベース) を取得
                volume = ticker.get('quoteVolume')
                if volume is not None and volume > 0:
                    usdt_future_tickers.append({
                        'symbol': symbol,
                        'volume': volume
                    })

        # 出来高降順でソート
        usdt_future_tickers.sort(key=lambda x: x['volume'], reverse=True)

        # トップNのシンボルを取得
        top_symbols = [t['symbol'] for t in usdt_future_tickers[:limit]]

        # デフォルトシンボルとマージし、重複を削除
        merged_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        # 最終的な監視リストから非アクティブな市場を削除
        final_monitor_list = []
        for symbol in merged_symbols:
             market_info = EXCHANGE_CLIENT.markets.get(symbol)
             if market_info and market_info.get('active', True):
                 final_monitor_list.append(symbol)
             else:
                 logging.info(f"ℹ️ {symbol} は非アクティブなため監視リストから除外しました。")
                 
        logging.info(f"✅ 出来高トップ {len(top_symbols)} 銘柄を取得し、デフォルトシンボルとマージしました。最終的な監視銘柄数: {len(final_monitor_list)}")
        return final_monitor_list

    except Exception as e:
        logging.error(f"❌ 出来高トップシンボルの取得に失敗しました。デフォルトシンボルを使用します: {e}", exc_info=True)
        return DEFAULT_SYMBOLS.copy()

async def fetch_ohlcv(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """
    指定されたシンボルのOHLCVデータを取得する。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error(f"❌ OHLCVデータ取得失敗: {symbol} - クライアントが準備できていません。")
        return None
        
    try:
        # MEXCの場合、params={'defaultType': 'swap'} が必要
        params = {'defaultType': 'swap'} if EXCHANGE_CLIENT.id == 'mexc' else {}
        
        # 💡 APIのレートリミットを考慮し、ランダムなディレイを挿入
        await asyncio.sleep(random.uniform(0.1, 0.5)) 

        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(
            symbol=symbol, 
            timeframe=timeframe, 
            limit=limit,
            params=params
        )
        
        if not ohlcv or len(ohlcv) < limit:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のデータが不足しています。必要数: {limit} / 取得数: {len(ohlcv) if ohlcv else 0}")
            return None

        # DataFrameに変換
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        
        # データ型の変換を明示 (CCXTからのデータは全てfloatとして扱う)
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col], errors='coerce')
        
        return df

    except ccxt.ExchangeError as e:
        logging.error(f"❌ {symbol} ({timeframe}) のデータ取得に失敗 (CCXT Exchange Error): {e}")
    except ccxt.NetworkError as e:
        logging.error(f"❌ {symbol} ({timeframe}) のデータ取得に失敗 (CCXT Network Error): {e}")
    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}) のデータ取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        
    return None

async def fetch_fgi_data() -> Dict:
    """
    Fear & Greed Index (FGI) データを取得し、Botで使用するプロキシ値を計算する。
    """
    global FGI_API_URL, GLOBAL_MACRO_CONTEXT, FGI_ACTIVE_THRESHOLD, FGI_SLUMP_THRESHOLD
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        
        if data and 'data' in data and len(data['data']) > 0:
            fgi_value = int(data['data'][0]['value'])
            fgi_value_classification = data['data'][0]['value_classification']
            
            # FGI (0:Extreme Fear -> 100:Extreme Greed) を -0.5 -> +0.5 の範囲に正規化
            # (FGI - 50) / 100 * 1.0 (50を0に、0を-0.5に、100を+0.5に)
            fgi_proxy = (fgi_value - 50) / 100.0
            
            # FGIプロキシ値をGLOBAL_MACRO_CONTEXTに更新
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value} ({fgi_value_classification})"

            logging.info(f"✅ FGIデータ取得: {GLOBAL_MACRO_CONTEXT['fgi_raw_value']}, プロキシ値: {fgi_proxy:+.4f}")
            
            return {
                'fgi_proxy': fgi_proxy, 
                'fgi_raw_value': GLOBAL_MACRO_CONTEXT['fgi_raw_value']
            }
        
        logging.warning("⚠️ FGIデータが空または無効です。")
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得エラー: {e}")
        
    return {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A'}


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame, symbol: str, timeframe: str) -> Dict[str, Any]:
    """
    テクニカル指標を計算し、結果を辞書として返す。
    """
    if df is None or df.empty or len(df) < LONG_TERM_SMA_LENGTH:
        logging.warning(f"⚠️ {symbol} ({timeframe}): データフレームが空か、長期SMA ({LONG_TERM_SMA_LENGTH}) に満たないためインジケーター計算をスキップ。")
        return {}

    try:
        # 1. 移動平均線 (SMA) - 長期トレンド
        df['SMA_Long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
        
        # 2. RSI (Relative Strength Index)
        df['RSI'] = ta.rsi(df['close'], length=14)
        
        # 3. MACD (Moving Average Convergence Divergence)
        macd_results = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd_results is not None and not macd_results.empty:
            df['MACD'] = macd_results['MACD_12_26_9']
            df['MACD_H'] = macd_results['MACDH_12_26_9']
            df['MACD_S'] = macd_results['MACDS_12_26_9']
        else:
             df['MACD'] = np.nan
             df['MACD_H'] = np.nan
             df['MACD_S'] = np.nan

        # 4. Bollinger Bands (BBANDS)
        bbands_results = ta.bbands(df['close'], length=20, std=2)
        if bbands_results is not None and not bbands_results.empty:
            df['BBL'] = bbands_results['BBL_20_2.0']
            df['BBM'] = bbands_results['BBM_20_2.0']
            df['BBU'] = bbands_results['BBU_20_2.0']
        else:
            df['BBL'] = np.nan
            df['BBM'] = np.nan
            df['BBU'] = np.nan
        
        # BB幅 (ボラティリティ測定に使用)
        df['BB_width'] = df['BBU'] - df['BBL']
        
        # 5. ATR (Average True Range) - リスク管理に使用
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
        
        # 6. OBV (On-Balance Volume) - 出来高モメンタム
        df['OBV'] = ta.obv(df['close'], df['volume'])
        
        # 7. StochRSI (Stochastic RSI) - 新規追加
        stochrsi_results = ta.stochrsi(df['close'], length=14, rsi_length=14, k=3, d=3)
        if stochrsi_results is not None and not stochrsi_results.empty:
            df['StochRSI_K'] = stochrsi_results['STOCHRSIk_14_14_3_3']
            df['StochRSI_D'] = stochrsi_results['STOCHRSId_14_14_3_3']
        else:
             df['StochRSI_K'] = np.nan
             df['StochRSI_D'] = np.nan
             
        # 8. ADX (Average Directional Index) - 新規追加
        adx_results = ta.adx(df['high'], df['low'], df['close'], length=14)
        if adx_results is not None and not adx_results.empty:
            df['ADX'] = adx_results['ADX_14']
            df['DMP'] = adx_results['DMP_14']
            df['DMN'] = adx_results['DMN_14']
        else:
             df['ADX'] = np.nan
             df['DMP'] = np.nan
             df['DMN'] = np.nan
             
        
        # 最後の行のインジケーター値を抽出
        last_index = df.index[-1]
        
        # ⚠️ nanのチェックを強化
        
        indicators = {
            'close': df.loc[last_index, 'close'],
            'open': df.loc[last_index, 'open'],
            'high': df.loc[last_index, 'high'],
            'low': df.loc[last_index, 'low'],
            'volume': df.loc[last_index, 'volume'],
            'SMA_Long': df.loc[last_index, 'SMA_Long'],
            'RSI': df.loc[last_index, 'RSI'],
            'MACD': df.loc[last_index, 'MACD'],
            'MACD_H': df.loc[last_index, 'MACD_H'],
            'MACD_S': df.loc[last_index, 'MACD_S'],
            'BBL': df.loc[last_index, 'BBL'],
            'BBM': df.loc[last_index, 'BBU'], # BBMではなくBBUの終値が必要だったため修正
            'BBU': df.loc[last_index, 'BBL'], # BBUではなくBBLの終値が必要だったため修正
            'BB_width': df.loc[last_index, 'BB_width'],
            'ATR': df.loc[last_index, 'ATR'],
            'OBV': df.loc[last_index, 'OBV'],
            'StochRSI_K': df.loc[last_index, 'StochRSI_K'],
            'StochRSI_D': df.loc[last_index, 'StochRSI_D'],
            'ADX': df.loc[last_index, 'ADX'],
            'DMP': df.loc[last_index, 'DMP'],
            'DMN': df.loc[last_index, 'DMN'],
            'raw_ohlcv': df.iloc[-1].to_dict() # 生のOHLCVデータも格納
        }
        
        # NA(NaN)値を0.0に置換 (スコアリングで利用するため)
        for key, value in indicators.items():
            if isinstance(value, (float, np.floating)) and np.isnan(value):
                indicators[key] = 0.0 
                
        # 💡 OBVの傾きを計算 (5期間の線形回帰)
        obv_slope = 0.0
        if len(df) >= 5 and not df['OBV'].isnull().all():
            try:
                # np.polyfitの戻り値エラー対策: dropna()で NaN の行を除外してから計算
                obv_data = df['OBV'].tail(5).dropna()
                if len(obv_data) >= 2: # 最低2点のデータが必要
                    # X軸を0から始める整数にする
                    x_indices = np.arange(len(obv_data))
                    # 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
                    # polyfitは係数と残差を返す。1次 (deg=1) の場合は [傾き, 切片] 
                    coeffs = np.polyfit(x_indices, obv_data.values, 1)
                    obv_slope = coeffs[0]
                else:
                    logging.warning(f"⚠️ {symbol} ({timeframe}): OBV傾き計算のためのデータが不足しています (取得可能数: {len(obv_data)})。")

            except Exception as e:
                logging.error(f"❌ {symbol} ({timeframe}): OBV傾き計算エラー: {e}")
                
        indicators['OBV_Slope'] = obv_slope
        
        # 💡 RSIダイバージェンスの存在チェック (簡易版)
        # 終値 (3期間) の傾きとRSI (3期間) の傾きの比較
        close_slope = 0.0
        rsi_slope = 0.0
        rsi_divergence_flag = False
        
        if len(df) >= 5:
            try:
                # 終値の傾き (3期間)
                close_data = df['close'].tail(3)
                if len(close_data) == 3:
                     coeffs_close = np.polyfit(np.arange(3), close_data.values, 1)
                     close_slope = coeffs_close[0]
                     
                # RSIの傾き (3期間)
                rsi_data = df['RSI'].tail(3).dropna()
                if len(rsi_data) == 3:
                     coeffs_rsi = np.polyfit(np.arange(3), rsi_data.values, 1)
                     rsi_slope = coeffs_rsi[0]
                     
                # 強気のダイバージェンス (価格が安値を更新、RSIは安値を切り上げ)
                if close_slope < 0 and rsi_slope > 0 and indicators['RSI'] < 40:
                    rsi_divergence_flag = True
                # 弱気のダイバージェンス (価格が高値を更新、RSIは高値を切り下げ)
                elif close_slope > 0 and rsi_slope < 0 and indicators['RSI'] > 60:
                    rsi_divergence_flag = True
                    
            except Exception as e:
                logging.error(f"❌ {symbol} ({timeframe}): RSIダイバージェンスチェックエラー: {e}")
        
        indicators['RSI_Divergence'] = rsi_divergence_flag


        return indicators

    except Exception as e:
        logging.error(f"❌ {symbol} ({timeframe}): インジケーター計算中にエラーが発生しました: {e}", exc_info=True)
        return {}


def score_signal(indicators: Dict[str, Any], symbol: str, timeframe: str, is_top_volume_symbol: bool, macro_context: Dict) -> List[Dict]:
    """
    計算されたテクニカル指標とマクロコンテキストに基づき、ロングとショートのスコアを計算する。
    スコアが一定の閾値に満たない場合は、シグナルを生成しない。
    """
    
    if not indicators or indicators.get('close') == 0.0 or indicators.get('ATR') == 0.0:
        return []
    
    close_price = indicators['close']
    current_time = time.time()
    
    # 総合スコアとボーナス/ペナルティ要因を格納する辞書
    tech_data = {
        'indicators': indicators,
        'long_term_reversal_penalty_value': 0.0,
        'rsi_momentum_bonus_value': 0.0,
        'macd_penalty_value': 0.0,
        'liquidity_bonus_value': 0.0,
        'sentiment_fgi_proxy_bonus': 0.0,
        'volatility_penalty_value': 0.0,
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, # ベーススコア扱い
        'obv_momentum_bonus_value': 0.0,
        'stoch_rsi_penalty_value': 0.0, # 🆕 StochRSI
        'adx_trend_bonus_value': 0.0,   # 🆕 ADX
        'rsi_divergence_bonus': 0.0,
        'fgi_proxy_value': macro_context.get('fgi_proxy', 0.0), # ログ用
        'stoch_rsi_k_value': indicators.get('StochRSI_K', np.nan), # ログ用
        'adx_raw_value': indicators.get('ADX', np.nan), # ログ用
    }
    
    long_score = BASE_SCORE
    short_score = BASE_SCORE
    
    # ----------------------------------------------------------------------
    # 1. 長期トレンド順張り/逆張りペナルティ (SMA_Long)
    # ----------------------------------------------------------------------
    sma_long = indicators['SMA_Long']
    
    if sma_long != 0.0:
        if close_price > sma_long:
            # 価格がSMAの上: ロングにボーナス (順張り)、ショートにペナルティ (逆張り)
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY
            long_score += LONG_TERM_REVERSAL_PENALTY
            short_score -= LONG_TERM_REVERSAL_PENALTY
        elif close_price < sma_long:
            # 価格がSMAの下: ショートにボーナス (順張り)、ロングにペナルティ (逆張り)
            tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY
            long_score -= LONG_TERM_REVERSAL_PENALTY
            short_score += LONG_TERM_REVERSAL_PENALTY
            
    # ----------------------------------------------------------------------
    # 2. RSI モメンタム加速ボーナス
    # ----------------------------------------------------------------------
    rsi = indicators['RSI']
    rsi_bonus = 0.0
    
    if 50 < rsi < (100 - RSI_MOMENTUM_LOW): # RSI 50-60
        # ロングのモメンタム加速
        rsi_bonus = 0.05
        long_score += rsi_bonus
    elif RSI_MOMENTUM_LOW < rsi < 50: # RSI 40-50
        # ショートのモメンタム加速
        rsi_bonus = 0.05
        short_score += rsi_bonus
        
    # RSIダイバージェンスボーナス (より強力なシグナルと見なす)
    if indicators.get('RSI_Divergence'):
        # ダイバージェンスの方向に基づいてボーナスを与える
        # 終値の傾きが負 (安値更新) でRSIの傾きが正 (安値切り上げ) の場合はロング優位
        if close_price < close_price * 0.999 and indicators['RSI'] < 40: # 簡易的な安値更新判定
             long_score += RSI_DIVERGENCE_BONUS
             tech_data['rsi_divergence_bonus'] = RSI_DIVERGENCE_BONUS
        # 終値の傾きが正 (高値更新) でRSIの傾きが負 (高値切り下げ) の場合はショート優位
        elif close_price > close_price * 1.001 and indicators['RSI'] > 60: # 簡易的な高値更新判定
             short_score += RSI_DIVERGENCE_BONUS
             tech_data['rsi_divergence_bonus'] = RSI_DIVERGENCE_BONUS
        
    tech_data['rsi_momentum_bonus_value'] = rsi_bonus
    
    # ----------------------------------------------------------------------
    # 3. MACD モメンタム一致/不一致ペナルティ
    # ----------------------------------------------------------------------
    macd_h = indicators['MACD_H']
    
    if macd_h != 0.0:
        if macd_h > 0:
            # MACDヒストグラムが上昇: ロングにボーナス、ショートにペナルティ
            tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY
            long_score += MACD_CROSS_PENALTY
            short_score -= MACD_CROSS_PENALTY
        elif macd_h < 0:
            # MACDヒストグラムが下降: ショートにボーナス、ロングにペナルティ
            tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY
            long_score -= MACD_CROSS_PENALTY
            short_score += MACD_CROSS_PENALTY
            
    # ----------------------------------------------------------------------
    # 4. OBV 出来高確証ボーナス
    # ----------------------------------------------------------------------
    obv_slope = indicators.get('OBV_Slope', 0.0)
    
    if obv_slope > 0:
        # OBVが上昇: ロングにボーナス
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        long_score += OBV_MOMENTUM_BONUS
    elif obv_slope < 0:
        # OBVが下降: ショートにボーナス
        tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS
        short_score += OBV_MOMENTUM_BONUS
        
    # ----------------------------------------------------------------------
    # 5. ボラティリティ過熱ペナルティ (BB幅)
    # ----------------------------------------------------------------------
    bb_width = indicators.get('BB_width', 0.0)
    
    # BB幅が価格の VOLATILITY_BB_PENALTY_THRESHOLD (例: 1%) を超える場合
    if close_price > 0 and (bb_width / close_price) > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty = -0.10 # 大きめのペナルティ
        long_score += volatility_penalty
        short_score += volatility_penalty # 両方にペナルティ
        tech_data['volatility_penalty_value'] = volatility_penalty

    # ----------------------------------------------------------------------
    # 6. 流動性ボーナス
    # ----------------------------------------------------------------------
    if is_top_volume_symbol:
        liquidity_bonus = LIQUIDITY_BONUS_MAX
        long_score += liquidity_bonus
        short_score += liquidity_bonus
        tech_data['liquidity_bonus_value'] = liquidity_bonus
    
    # ----------------------------------------------------------------------
    # 7. マクロコンテキスト (FGI) 影響
    # ----------------------------------------------------------------------
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # FGIプロキシ値をそのままボーナス/ペナルティとして適用 (最大 FGI_PROXY_BONUS_MAX)
    fgi_score_influence = fgi_proxy * (FGI_PROXY_BONUS_MAX / 0.5) 
    fgi_score_influence = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_score_influence))
    
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_score_influence

    if fgi_score_influence > 0:
        # FGIがGreed (リスクオン): ロングにボーナス
        long_score += fgi_score_influence
    elif fgi_score_influence < 0:
        # FGIがFear (リスクオフ): ショートにボーナス
        short_score -= fgi_score_influence # 負の値を引く = 正の値を加算
        
    # ----------------------------------------------------------------------
    # 8. 構造的優位性 (ベース)
    # ----------------------------------------------------------------------
    # BASE_SCOREに含まれているが、内訳のために再加算
    long_score += STRUCTURAL_PIVOT_BONUS
    short_score += STRUCTURAL_PIVOT_BONUS

    # ----------------------------------------------------------------------
    # 9. 🆕 StochRSI 過熱ペナルティ/回復ボーナス
    # ----------------------------------------------------------------------
    stoch_k = indicators.get('StochRSI_K', np.nan)
    stoch_d = indicators.get('StochRSI_D', np.nan)
    stoch_penalty = 0.0
    
    if not np.isnan(stoch_k) and not np.isnan(stoch_d):
        if stoch_k > (100 - STOCHRSI_BOS_LEVEL): # 買われ過ぎ水準
            # ロングに対してペナルティ、ショートに対して回復ボーナス (逆張り)
            stoch_penalty = -STOCHRSI_BOS_PENALTY
            long_score += stoch_penalty 
            # ショートに対しては、過熱状態からの反転を期待してボーナス
            short_score += (STOCHRSI_BOS_PENALTY / 2.0) 
            
        elif stoch_k < STOCHRSI_BOS_LEVEL: # 売られ過ぎ水準
            # ショートに対してペナルティ、ロングに対して回復ボーナス (逆張り)
            stoch_penalty = -STOCHRSI_BOS_PENALTY
            short_score += stoch_penalty
            # ロングに対しては、過熱状態からの反転を期待してボーナス
            long_score += (STOCHRSI_BOS_PENALTY / 2.0)
            
        tech_data['stoch_rsi_penalty_value'] = stoch_penalty


    # ----------------------------------------------------------------------
    # 10. 🆕 ADX トレンド確証ボーナス
    # ----------------------------------------------------------------------
    adx = indicators.get('ADX', 0.0)
    dmp = indicators.get('DMP', 0.0)
    dmn = indicators.get('DMN', 0.0)
    
    adx_bonus = 0.0
    
    if adx >= ADX_TREND_STRENGTH_THRESHOLD:
        if dmp > dmn:
            # ADXが高く、+DI > -DI: 強気トレンド確証 (ロングにボーナス)
            adx_bonus = ADX_TREND_BONUS
            long_score += adx_bonus
        elif dmn > dmp:
            # ADXが高く、-DI > +DI: 弱気トレンド確証 (ショートにボーナス)
            adx_bonus = ADX_TREND_BONUS
            short_score += adx_bonus
            
    tech_data['adx_trend_bonus_value'] = adx_bonus
    
    
    # ----------------------------------------------------------------------
    # 11. RR比率とストップロスの計算 (ATRベース)
    # ----------------------------------------------------------------------
    atr = indicators.get('ATR', 0.0)
    
    # ATRが0.0または極端に小さい場合はスキップ
    if atr <= 0.0001:
        logging.warning(f"⚠️ {symbol} ({timeframe}): ATRがゼロまたは小さすぎます。シグナルをスキップ。")
        return []

    # ATRベースのストップロス幅 (ドル単位)
    atr_risk_dollars = atr * ATR_MULTIPLIER_SL
    
    # 最低リスク幅 (価格の MIN_RISK_PERCENT 相当) を確保
    min_risk_dollars = close_price * MIN_RISK_PERCENT
    risk_dollars = max(atr_risk_dollars, min_risk_dollars)
    
    # リワード幅の計算 (RR_RATIO_TARGETに基づく)
    reward_dollars = risk_dollars * RR_RATIO_TARGET

    signals = []
    
    # Longシグナル
    long_sl = close_price - risk_dollars
    long_tp = close_price + reward_dollars
    long_liq = calculate_liquidation_price(close_price, LEVERAGE, 'long', MIN_MAINTENANCE_MARGIN_RATE)
    
    # Shortシグナル
    short_sl = close_price + risk_dollars
    short_tp = close_price - reward_dollars
    short_liq = calculate_liquidation_price(close_price, LEVERAGE, 'short', MIN_MAINTENANCE_MARGIN_RATE)
    
    
    # スコアの正規化と丸め (0.0 から 1.0)
    long_score = max(0.0, min(1.0, long_score))
    short_score = max(0.0, min(1.0, short_score))
    
    # スコアが一定の閾値を超えている場合のみシグナルを生成
    current_threshold = get_current_threshold(macro_context)
    
    if long_score >= current_threshold:
        signals.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': 'long',
            'score': long_score,
            'rr_ratio': RR_RATIO_TARGET,
            'entry_price': close_price,
            'stop_loss': long_sl,
            'take_profit': long_tp,
            'liquidation_price': long_liq,
            'tech_data': tech_data,
            'timestamp': current_time,
        })

    if short_score >= current_threshold:
        signals.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': 'short',
            'score': short_score,
            'rr_ratio': RR_RATIO_TARGET,
            'entry_price': close_price,
            'stop_loss': short_sl,
            'take_profit': short_tp,
            'liquidation_price': short_liq,
            'tech_data': tech_data,
            'timestamp': current_time,
        })
        
    # ⚠️ デバッグ用: 閾値未満でもスコアをログに記録
    if not signals:
         max_score = max(long_score, short_score)
         best_side = 'long' if long_score > short_score else 'short'
         logging.info(f"ℹ️ {symbol} ({timeframe}): 最高スコア {max_score*100:.2f} (閾値 {current_threshold*100:.2f} 未満) のためシグナルをスキップしました。")
        
    return signals


async def run_full_analysis() -> List[Dict]:
    """
    全監視銘柄に対し、指定された全ての時間軸でテクニカル分析を実行し、シグナルを生成する。
    """
    global CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT
    
    if not IS_CLIENT_READY:
        logging.error("❌ クライアントが準備できていないため、分析をスキップします。")
        return []
        
    logging.info("🔬 全監視銘柄に対してフルテクニカル分析を開始します...")

    all_signals: List[Dict] = []
    
    # 出来高トップシンボルリストを最新の情報で更新
    top_volume_symbols = await fetch_top_volume_symbols(TOP_SYMBOL_LIMIT)
    CURRENT_MONITOR_SYMBOLS = top_volume_symbols
    
    # マクロコンテキストの取得
    await fetch_fgi_data()
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 各シンボルに対して非同期で処理を実行
    analysis_tasks = []
    
    for symbol in CURRENT_MONITOR_SYMBOLS:
        # このシンボルがトップ出来高リストに含まれているか
        is_top_volume_symbol = symbol in top_volume_symbols
        
        # 各時間枠でのOHLCV取得と分析タスクを作成
        for tf in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
            
            async def analyze_symbol_timeframe(sym, timeframe, limit, is_top_vol):
                try:
                    df = await fetch_ohlcv(sym, timeframe, limit)
                    if df is None:
                        return []
                    
                    indicators = calculate_technical_indicators(df, sym, timeframe)
                    if not indicators:
                        return []
                        
                    return score_signal(indicators, sym, timeframe, is_top_vol, GLOBAL_MACRO_CONTEXT)
                except Exception as e:
                    logging.error(f"❌ {sym} ({timeframe}) の分析中にエラー: {e}")
                    return []

            analysis_tasks.append(
                analyze_symbol_timeframe(symbol, tf, limit, is_top_volume_symbol)
            )

    # 全ての分析タスクを並行実行
    results = await asyncio.gather(*analysis_tasks)
    
    for signals in results:
        all_signals.extend(signals)

    # フィルタリング: スコアが閾値を超えているもののみを残す
    final_signals = [s for s in all_signals if s['score'] >= current_threshold]
    
    # 全ての分析結果 (閾値未満も含む) をグローバルに保存 (定時報告用)
    global LAST_ANALYSIS_SIGNALS
    LAST_ANALYSIS_SIGNALS = all_signals 

    logging.info(f"✅ フル分析完了。合計シグナル数 (閾値以上): {len(final_signals)} 件 (全分析結果: {len(all_signals)} 件)。")
    return final_signals

# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

async def check_for_entry_signals(signals: List[Dict]) -> List[Dict]:
    """
    シグナルリストを評価し、実際にエントリーするシグナルを決定する。
    - スコアの高い順にソート。
    - クールダウン期間をチェック。
    - 最大許容ポジション数 (TOP_SIGNAL_COUNT) を超えないかチェック。
    """
    global LAST_SIGNAL_TIME, OPEN_POSITIONS
    
    if TEST_MODE:
        logging.warning("⚠️ TEST_MODE: エントリーシグナルチェックを実行しますが、取引は実行されません。")
        
    if not signals:
        return []

    current_time = time.time()
    
    # 1. スコアの高い順にソート
    signals.sort(key=lambda x: x['score'], reverse=True)
    
    entry_candidates = []
    
    # 2. クールダウン期間と重複ポジションのチェック
    for signal in signals:
        symbol = signal['symbol']
        side = signal['side']
        signal_key = f"{symbol}_{side}"
        
        # 既に同銘柄でポジションを持っているかチェック
        has_position = any(p['symbol'] == symbol for p in OPEN_POSITIONS)
        if has_position:
            # ポジションがある場合は、シグナルをスキップ
            logging.info(f"ℹ️ {symbol} ({side}) は既にポジションを管理中のため、エントリーをスキップします。")
            continue
            
        # クールダウン期間のチェック
        last_signal_time = LAST_SIGNAL_TIME.get(signal_key, 0.0)
        if (current_time - last_signal_time) < TRADE_SIGNAL_COOLDOWN:
            logging.info(f"ℹ️ {symbol} ({side}) はクールダウン期間中のため、エントリーをスキップします。")
            continue
        
        entry_candidates.append(signal)
        
    # 3. 最大許容ポジション数のチェック
    # 現在のオープンポジション数
    current_open_count = len(OPEN_POSITIONS)
    # 新規に開くことができるポジション数
    max_new_entries = TOP_SIGNAL_COUNT - current_open_count
    
    if max_new_entries <= 0:
        logging.warning(f"⚠️ 最大許容ポジション数 ({TOP_SIGNAL_COUNT}) に達しているため、新規エントリーはスキップされます。")
        return []

    # 実行するエントリーシグナル
    entry_signals = entry_candidates[:max_new_entries]
    
    # 4. 採用されたシグナルのクールダウン時間を更新
    for signal in entry_signals:
        signal_key = f"{signal['symbol']}_{signal['side']}"
        LAST_SIGNAL_TIME[signal_key] = current_time
        
    if entry_signals:
        logging.info(f"🚀 {len(entry_signals)} 件の新規エントリーシグナルを採用しました。")
    
    return entry_signals


async def execute_trade(signal: Dict) -> Dict:
    """
    実際の取引注文を執行する。
    - ポジションのロットサイズ (契約数) を計算。
    - 成行注文を出す。
    - SL/TPの注文 (ワンキャンセルオーダー) を設定する (取引所がサポートしている場合)。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, FIXED_NOTIONAL_USDT
    
    if TEST_MODE:
        # テストモードではダミーの成功レスポンスを返す
        logging.warning(f"⚠️ TEST_MODE: {signal['symbol']} ({signal['side']}) の取引をシミュレートします。")
        return {
            'status': 'ok',
            'filled_amount': FIXED_NOTIONAL_USDT / signal['entry_price'] if signal['entry_price'] > 0 else 0.0,
            'filled_usdt': FIXED_NOTIONAL_USDT,
            'entry_price': signal['entry_price'],
            'order_id': f"TEST_ORDER_{uuid.uuid4().hex[:8]}",
            'side': signal['side'],
        }
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}

    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    
    # 1. ロットサイズ (契約数) の計算
    # 固定ロット金額 (FIXED_NOTIONAL_USDT) を使用
    if entry_price <= 0:
        return {'status': 'error', 'error_message': 'エントリー価格が不正です (0以下)。'}
        
    # 必要証拠金 = 名目価値 / レバレッジ
    # ここでは名目価値 (契約サイズ) を計算: 契約サイズ = FIXED_NOTIONAL_USDT / エントリー価格
    contracts_amount = FIXED_NOTIONAL_USDT / entry_price
    
    if contracts_amount <= 0:
        return {'status': 'error', 'error_message': '契約数量が0以下になります。'}
        
    # CCXTの buy/sell で必要な数量は、基軸通貨 (BTC/ETHなど) の量
    # amount = contracts_amount
    # buy: side='long' (買い)、sell: side='short' (売り)
    
    order_side = 'buy' if side == 'long' else 'sell'
    
    # 2. 成行注文 (Market Order) の執行
    trade_result = None
    order_params = {}

    try:
        # MEXCの場合、postOnly=False は不要だが、保険として設定
        if EXCHANGE_CLIENT.id == 'mexc':
            order_params['postOnly'] = False
            # MEXCは openType (2: Cross) を指定する必要がある
            order_params['openType'] = 2
            
            # set_leverageで設定済みのため、ここでは指定不要の可能性あり

        trade_result = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=order_side,
            amount=contracts_amount,
            params=order_params
        )
        
        # 3. 約定結果の確認と整形
        if trade_result and trade_result.get('status') == 'closed':
            # 注文が完全に約定した場合
            filled_amount = trade_result.get('filled', contracts_amount)
            # 実際のエントリー価格 (平均約定価格)
            actual_entry_price = trade_result.get('price', entry_price)
            filled_usdt_notional = filled_amount * actual_entry_price
            
            logging.info(f"✅ 約定成功: {symbol} {side} {filled_amount:.4f} @ {actual_entry_price:.4f} (Lot: {filled_usdt_notional:.2f} USDT)")
            
            return {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'entry_price': actual_entry_price,
                'order_id': trade_result.get('id'),
                'side': side,
            }
        else:
            # 部分約定またはステータス不明
            logging.warning(f"⚠️ 約定結果が不明確です: {symbol} {side} - ステータス: {trade_result.get('status', 'N/A')}")
            return {'status': 'error', 'error_message': f'約定ステータスが不明確: {trade_result.get("status", "N/A")}'}

    except ccxt.ExchangeError as e:
        error_msg = str(e)
        # 'Amount can not be less than zero' 注文数量が小さすぎるときのMEXCエラー
        if 'Amount can not be less than zero' in error_msg:
             # このエラーは、契約数が最小ロットを下回った場合に発生する可能性が高い
             logging.error(f"❌ 注文失敗 (最小ロット以下): {symbol} {side} - 必要ロット: {contracts_amount:.6f}。{e}")
             return {'status': 'error', 'error_message': f'最小ロット以下: {contracts_amount:.6f}'}
             
        logging.error(f"❌ 注文失敗 (CCXT Exchange Error): {symbol} {side} - {e}")
        return {'status': 'error', 'error_message': f'取引所エラー: {error_msg}'}
    except ccxt.NetworkError as e:
        logging.error(f"❌ 注文失敗 (CCXT Network Error): {symbol} {side} - {e}")
        return {'status': 'error', 'error_message': f'ネットワークエラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 注文失敗 (予期せぬエラー): {symbol} {side} - {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


async def place_stop_take_profit_orders(position: Dict) -> Dict:
    """
    ポジションに対して、SL/TPのオーダー (O.C.O.または同等の機能) を設定する。
    MEXCでは、positionSide を使用した SL/TP 設定が可能。
    """
    global EXCHANGE_CLIENT
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {position['symbol']} のSL/TP注文をシミュレートします。")
        return {'status': 'ok', 'message': 'SL/TP注文設定をシミュレートしました。'}

    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'message': 'CCXTクライアントが準備できていません。'}

    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    stop_loss = position['stop_loss']
    take_profit = position['take_profit']
    entry_price = position['entry_price']
    
    # 決済のための注文方向 (ロングの決済は'sell', ショートの決済は'buy')
    close_side = 'sell' if side == 'long' else 'buy'
    
    # OCO (One-Cancels-the-Other) や SL/TP 設定は取引所ごとに異なる
    # MEXCの場合、ポジションベースの stopLossPrice/takeProfitPrice が利用可能 (create_orderでは難しい)
    
    # 💡 シンプルに、トリガー価格 (Stop Limit/Stop Market) を個別に設定する
    
    sl_order_result = None
    tp_order_result = None
    
    # -----------------------------------------------------------
    # 1. ストップロス (SL) の設定
    # -----------------------------------------------------------
    # SLは Stop Market (トリガー価格に達したら成行で決済) を使用
    try:
        if stop_loss > 0.0:
            
            sl_params = {}
            if EXCHANGE_CLIENT.id == 'mexc':
                 # MEXCでトリガー価格を指定する際は 'stopLossPrice' を使用
                 sl_params['stopLossPrice'] = stop_loss
                 # MEXCのTP/SLは create_orderではなく edit_position を使用する方が一般的だが、
                 # CCXTのラッパーが利用できない場合があるため、ここでは create_order (Stop Limit/Market) で対応する
                 # 'stop' タイプを使用する
                 sl_order_result = await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type='stop_market', 
                    side=close_side,
                    amount=contracts,
                    price=stop_loss, # Stop Market では価格はトリガー価格として扱われる
                    params=sl_params
                )
            else:
                # 他の取引所向け (統一的な stop_market を使用)
                sl_order_result = await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type='stop_market', 
                    side=close_side,
                    amount=contracts,
                    stop_price=stop_loss,
                    params=sl_params
                )
            logging.info(f"✅ {symbol} SL注文設定: {close_side} {contracts:.4f} @ {stop_loss:.4f} (Stop Market)")
            
    except Exception as e:
        logging.error(f"❌ {symbol} SL注文設定失敗: {e}")

    # -----------------------------------------------------------
    # 2. テイクプロフィット (TP) の設定
    # -----------------------------------------------------------
    # TPも Stop Market (または Take Profit Market) を使用
    try:
        if take_profit > 0.0 and ((side == 'long' and take_profit > entry_price) or (side == 'short' and take_profit < entry_price)):
            
            tp_params = {}
            if EXCHANGE_CLIENT.id == 'mexc':
                # MEXCでトリガー価格を指定する際は 'takeProfitPrice' を使用
                tp_params['takeProfitPrice'] = take_profit
                
                tp_order_result = await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type='take_profit_market', # Take Profit Market を使用
                    side=close_side,
                    amount=contracts,
                    price=take_profit, 
                    params=tp_params
                )
            else:
                # 他の取引所向け (統一的な take_profit_market を使用)
                tp_order_result = await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type='take_profit_market', 
                    side=close_side,
                    amount=contracts,
                    stop_price=take_profit,
                    params=tp_params
                )
                
            logging.info(f"✅ {symbol} TP注文設定: {close_side} {contracts:.4f} @ {take_profit:.4f} (Take Profit Market)")
            
    except Exception as e:
        logging.error(f"❌ {symbol} TP注文設定失敗: {e}")
        
    return {
        'status': 'ok', 
        'sl_order': sl_order_result, 
        'tp_order': tp_order_result
    }

async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    """
    単一のシグナルに基づいてエントリー処理全体を実行する。
    """
    
    # 1. 実際の取引執行
    trade_result = await execute_trade(signal)
    
    if trade_result['status'] != 'ok':
        log_signal(signal, "エントリー失敗", trade_result)
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        await send_telegram_notification(format_telegram_message(signal, "取引シグナル", current_threshold, trade_result))
        return None
        
    # 2. ポジション情報の作成 (SL/TP情報と約定情報をマージ)
    new_position = {
        'symbol': signal['symbol'],
        'side': signal['side'],
        'contracts': trade_result['filled_amount'],
        'filled_usdt': trade_result['filled_usdt'],
        'entry_price': trade_result['entry_price'],
        'stop_loss': signal['stop_loss'],
        'take_profit': signal['take_profit'],
        'liquidation_price': signal['liquidation_price'],
        'score': signal['score'],
        'rr_ratio': signal['rr_ratio'],
        'entry_time': time.time(),
        'order_id': trade_result['order_id'],
        'tech_data': signal['tech_data'], # 分析データも保持
    }
    
    # 3. ポジションをグローバルリストに追加
    global OPEN_POSITIONS
    OPEN_POSITIONS.append(new_position)
    
    logging.info(f"✅ 新規ポジションを管理リストに追加: {new_position['symbol']} ({new_position['side']})")
    
    # 4. SL/TP注文の設定 (非同期だが待つ)
    await place_stop_take_profit_orders(new_position)
    
    # 5. 通知とログ
    # シグナル辞書に実際の約定情報を追加して通知メッセージを整形
    signal_for_notify = {**signal, 'entry_price': new_position['entry_price'], 'liquidation_price': new_position['liquidation_price']}
    
    log_signal(signal_for_notify, "エントリー成功", trade_result)
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    await send_telegram_notification(format_telegram_message(signal_for_notify, "取引シグナル", current_threshold, trade_result))

    return new_position

async def close_position(position: Dict, exit_type: str) -> Dict:
    """
    指定されたポジションを成行で決済する。
    """
    global EXCHANGE_CLIENT
    
    symbol = position['symbol']
    side = position['side']
    contracts = position['contracts']
    entry_price = position['entry_price']
    
    close_side = 'sell' if side == 'long' else 'buy'
    
    exit_result = {
        'status': 'error',
        'error_message': '初期化失敗',
        'entry_price': entry_price,
        'exit_price': 0.0,
        'pnl_usdt': 0.0,
        'pnl_rate': 0.0,
        'exit_type': exit_type,
        'filled_amount': contracts
    }
    
    if TEST_MODE:
        # テストモードではダミーの決済シミュレーション
        logging.warning(f"⚠️ TEST_MODE: {symbol} ({side}) の決済をシミュレートします。Exit: {exit_type}")
        
        # SL/TPトリガー価格を決済価格として使用
        if exit_type == 'STOP_LOSS':
             exit_price = position['stop_loss']
        elif exit_type == 'TAKE_PROFIT':
             exit_price = position['take_profit']
        else:
             # 現在の終値 (ここでは簡易的にエントリー価格付近)
             exit_price = entry_price * (1.0002 if side == 'long' else 0.9998) 
             
        pnl_usdt = contracts * (exit_price - entry_price) if side == 'long' else contracts * (entry_price - exit_price)
        pnl_rate = pnl_usdt / (position['filled_usdt'] / LEVERAGE) if position['filled_usdt'] > 0 else 0.0 # 証拠金に対するリターン
        
        exit_result.update({
            'status': 'ok',
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'order_id': f"TEST_CLOSE_{uuid.uuid4().hex[:8]}",
        })
        
        return exit_result


    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        exit_result['error_message'] = 'CCXTクライアントが準備できていません。'
        return exit_result

    try:
        # 1. 既存のSL/TP注文をキャンセル
        # MEXCなどでは、ポジションをクローズすると自動キャンセルされることが多いが、念のため
        # CCXTの fetch_open_orders() で取得し、キャンセルするロジックが必要だが、
        # 取引所APIごとに複雑なため、ここでは一旦スキップし、取引所側の自動キャンセルに依存する
        logging.info(f"ℹ️ {symbol} の既存のSL/TP注文のキャンセルを試みます (取引所依存)。")
        
        # 2. ポジションをクローズする成行注文
        order_params = {}
        if EXCHANGE_CLIENT.id == 'mexc':
            # ポジションクローズを明示
            order_params['closePosition'] = True
            order_params['openType'] = 2 # Cross Marginを指定
        
        trade_result = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=close_side,
            amount=contracts,
            params=order_params
        )

        # 3. 約定結果の確認と整形
        if trade_result and trade_result.get('status') == 'closed':
            exit_price = trade_result.get('price', 0.0)
            
            # PnLの計算 (概算)
            pnl_usdt = contracts * (exit_price - entry_price) if side == 'long' else contracts * (entry_price - exit_price)
            # 証拠金に対するリターン率
            pnl_rate = pnl_usdt / (position['filled_usdt'] / LEVERAGE) if position['filled_usdt'] > 0 else 0.0

            logging.info(f"✅ ポジション決済成功: {symbol} {side} @ {exit_price:.4f} - PnL: {pnl_usdt:+.4f} USDT")

            exit_result.update({
                'status': 'ok',
                'exit_price': exit_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'order_id': trade_result.get('id'),
            })
        else:
            logging.error(f"❌ ポジション決済失敗: {symbol} - ステータス: {trade_result.get('status', 'N/A')}")
            exit_result['error_message'] = f'決済ステータスが不明確: {trade_result.get("status", "N/A")}'

    except ccxt.ExchangeError as e:
        logging.error(f"❌ ポジション決済失敗 (CCXT Exchange Error): {symbol} - {e}")
        exit_result['error_message'] = f'取引所エラー: {e}'
    except Exception as e:
        logging.error(f"❌ ポジション決済失敗 (予期せぬエラー): {symbol} - {e}", exc_info=True)
        exit_result['error_message'] = f'予期せぬエラー: {e}'

    return exit_result

async def check_and_manage_open_positions():
    """
    オープンポジションを監視し、SL/TPに達したか、または強制清算されていないかを確認する。
    """
    global OPEN_POSITIONS, EXCHANGE_CLIENT, IS_CLIENT_READY
    
    if not IS_CLIENT_READY:
        return
        
    if not OPEN_POSITIONS:
        logging.debug("ℹ️ 管理中のオープンポジションはありません。")
        return

    logging.info(f"👀 {len(OPEN_POSITIONS)} 件のオープンポジションのSL/TPチェックを開始します。")

    # 1. CCXTから最新のポジション情報を取得
    account_status = await fetch_account_status()
    ccxt_open_positions = account_status['open_positions']
    
    # シンボルをキーとするディクショナリに変換
    ccxt_pos_map = {pos['symbol']: pos for pos in ccxt_open_positions}
    
    positions_to_remove = []
    
    # 2. 管理中のポジションをループ
    for i, managed_pos in enumerate(OPEN_POSITIONS):
        symbol = managed_pos['symbol']
        side = managed_pos['side']
        sl = managed_pos['stop_loss']
        tp = managed_pos['take_profit']
        
        # 2.1. 強制清算/外部決済のチェック
        if symbol not in ccxt_pos_map:
            # CCXTのポジションリストにない = 外部で決済された可能性が高い
            # (SL/TPトリガー、または強制清算など)
            
            # 強制清算されたかどうかの判定は難しいため、ここでは「外部決済」として処理
            
            # 💡 最後のキャンドル価格を取得し、SL/TPに近かったかを確認する簡易ロジック
            ohlcv_df = await fetch_ohlcv(symbol, '1m', 1)
            last_price = ohlcv_df['close'].iloc[-1] if ohlcv_df is not None and not ohlcv_df.empty else None
            
            exit_type = "EXTERNAL_CLOSE"
            
            if last_price is not None:
                if side == 'long' and last_price <= sl:
                    exit_type = "STOP_LOSS_EXTERNAL"
                elif side == 'long' and last_price >= tp:
                    exit_type = "TAKE_PROFIT_EXTERNAL"
                elif side == 'short' and last_price >= sl:
                    exit_type = "STOP_LOSS_EXTERNAL"
                elif side == 'short' and last_price <= tp:
                    exit_type = "TAKE_PROFIT_EXTERNAL"
                    
            logging.warning(f"⚠️ {symbol} ({side}) ポジションがCCXTリストに見つかりません。外部決済 (推定: {exit_type}) として処理します。")

            # 外部決済されたポジションをリストから削除し、ログと通知のみ行う
            positions_to_remove.append(i)
            
            # ログと通知 (決済価格は正確ではない可能性があるため、entry_priceを使用)
            exit_result_dummy = {
                'status': 'ok',
                'exit_price': last_price if last_price is not None else managed_pos['entry_price'],
                'entry_price': managed_pos['entry_price'],
                'pnl_usdt': 0.0, # 外部決済のため不明
                'pnl_rate': 0.0, # 外部決済のため不明
                'exit_type': exit_type,
                'filled_amount': managed_pos['contracts']
            }
            
            log_signal(managed_pos, "ポジション決済", exit_result_dummy)
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            await send_telegram_notification(format_telegram_message(managed_pos, "ポジション決済", current_threshold, exit_result_dummy, exit_type))
            
            continue # 次のポジションへ

        # 2.2. SL/TPに達しているか (Botのロジックで再確認)
        
        # 最新の終値を取得 (1mで十分)
        ohlcv_df = await fetch_ohlcv(symbol, '1m', 1)
        if ohlcv_df is None or ohlcv_df.empty:
            logging.warning(f"⚠️ {symbol} の最新価格を取得できませんでした。SL/TPチェックをスキップします。")
            continue
            
        last_price = ohlcv_df['close'].iloc[-1]
        
        exit_type = None
        
        if side == 'long':
            if last_price <= sl:
                exit_type = "STOP_LOSS"
            elif last_price >= tp:
                exit_type = "TAKE_PROFIT"
        elif side == 'short':
            if last_price >= sl:
                exit_type = "STOP_LOSS"
            elif last_price <= tp:
                exit_type = "TAKE_PROFIT"
                
        # 2.3. ポジションの決済実行
        if exit_type:
            logging.info(f"🚨 {symbol} ({side}) で {exit_type} トリガーを確認しました。決済を実行します。")
            
            # 決済実行
            exit_result = await close_position(managed_pos, exit_type)
            
            if exit_result['status'] == 'ok':
                positions_to_remove.append(i)
                
                # ログと通知
                log_signal(managed_pos, "ポジション決済", exit_result)
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                await send_telegram_notification(format_telegram_message(managed_pos, "ポジション決済", current_threshold, exit_result, exit_type))
            else:
                logging.error(f"❌ {symbol} の決済に失敗しました: {exit_result['error_message']}")


    # 3. 決済済みポジションをグローバルリストから削除
    # 逆順に削除することでインデックスのずれを防ぐ
    for index in sorted(positions_to_remove, reverse=True):
        removed_pos = OPEN_POSITIONS.pop(index)
        logging.info(f"🗑️ ポジションを管理リストから削除しました: {removed_pos['symbol']} ({removed_pos['side']})")


# ====================================================================================
# MAIN SCHEDULERS
# ====================================================================================

async def main_bot_scheduler():
    """メインのボットスケジューラ: 分析、シグナル生成、取引執行"""
    global LAST_SUCCESS_TIME, LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL, IS_FIRST_MAIN_LOOP_COMPLETED

    logging.info("⏳ メインボットスケジューラを開始します。")
    
    # 1. 初期設定とアカウントステータスの取得
    if not await initialize_exchange_client():
         # 致命的なエラーのため、Botを停止する (または再試行ロジックを実装する)
         # ここでは致命的エラーとして通知
         await send_telegram_notification("🔥 <b>Apex BOT 起動失敗 (致命的エラー)</b>\nCCXTクライアントの初期化に失敗しました。APIキー/設定を確認してください。")
         return 

    account_status = await fetch_account_status()
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 2. BOT起動通知
    startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
    await send_telegram_notification(startup_message)


    while True:
        try:
            current_time = time.time()
            
            # -----------------------------------------------------------
            # 2. 分析とシグナル生成
            # -----------------------------------------------------------
            logging.info(f"--- 🔄 メイン分析ループ開始: {datetime.now(JST).strftime('%H:%M:%S')} ---")
            
            # フル分析の実行 (FGIと出来高の更新を含む)
            signals = await run_full_analysis()
            
            # 閾値未満の最高スコアを定期的に通知 (run_full_analysis内でLAST_ANALYSIS_SIGNALSが更新される)
            await notify_highest_analysis_score()

            # -----------------------------------------------------------
            # 3. エントリーシグナルの評価と取引執行
            # -----------------------------------------------------------
            
            entry_signals = await check_for_entry_signals(signals)
            
            if entry_signals:
                logging.info(f"💰 {len(entry_signals)} 件の新規エントリーシグナルを処理します。")
                
                entry_tasks = []
                for signal in entry_signals:
                    # エントリータスクを並行実行
                    entry_tasks.append(process_entry_signal(signal))
                
                # エントリータスクの完了を待機
                await asyncio.gather(*entry_tasks)

            else:
                logging.info("ℹ️ 新規エントリーシグナルはありませんでした。")

            # -----------------------------------------------------------
            # 4. WebShareへのデータアップロード (定期実行)
            # -----------------------------------------------------------
            if (current_time - LAST_WEBSHARE_UPLOAD_TIME) >= WEBSHARE_UPLOAD_INTERVAL:
                
                # 最新の口座ステータスを再度取得 (ポジションの影響を反映させるため)
                account_status = await fetch_account_status()
                
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'global_macro': GLOBAL_MACRO_CONTEXT,
                    'open_positions': OPEN_POSITIONS,
                    'last_analysis_signals': LAST_ANALYSIS_SIGNALS, # 全分析結果を送信
                    'current_threshold': get_current_threshold(GLOBAL_MACRO_CONTEXT),
                    'monitoring_symbols': CURRENT_MONITOR_SYMBOLS,
                    # ... その他の必要なデータ ...
                }
                
                await send_webshare_update(webshare_data)
                
                LAST_WEBSHARE_UPLOAD_TIME = current_time # 💡 グローバル変数を更新
                
            # -----------------------------------------------------------
            
            LAST_SUCCESS_TIME = current_time
            IS_FIRST_MAIN_LOOP_COMPLETED = True # 初回ループ完了
            
            logging.info(f"--- ✅ メイン分析ループ完了。次の実行まで {LOOP_INTERVAL} 秒待機 ---")
            await asyncio.sleep(LOOP_INTERVAL)
            
        except Exception as e:
            logging.critical(f"❌ メインボットスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            # 致命的なエラーが発生した場合、一定時間待機してから再試行
            await send_telegram_notification(f"🔥 <b>Apex BOT 致命的エラー発生</b>\nメインループがクラッシュしました。再試行します。エラー: <code>{e}</code>")
            await asyncio.sleep(LOOP_INTERVAL * 2) # 長めに待機
            await initialize_exchange_client() # クライアントを再初期化


async def position_monitor_scheduler():
    """ポジション監視スケジューラ: SL/TPのチェックと決済"""
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
    logging.error(f"捕捉されなかった例外が発生しました: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error"},
    )


# ====================================================================================
# ENTRY POINT
# ====================================================================================

if __name__ == "__main__":
    # uvicorn.run(app, host="0.0.0.0", port=8000)
    # 💡 致命的エラー修正: uvicornの起動オプションを環境変数または明示的な設定で
    HOST = os.getenv("API_HOST", "0.0.0.0")
    PORT = int(os.getenv("API_PORT", 8000))
    
    # uvicorn.run はブロッキング呼び出しであり、このプロセスがメインとなる
    # ここでは、FastAPIを起動して、非同期タスクとしてスケジューラを実行する
    uvicorn.run(app, host=HOST, port=PORT)
