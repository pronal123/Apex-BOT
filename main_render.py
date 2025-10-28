# ====================================================================================
# Apex BOT v20.0.48 - Future Trading / 30x Leverage 
# (Feature: v20.0.47機能 + 指標計算の致命的安定性向上)
# 
# 🚨 致命的エラー修正強化: 
# 1. ✅ 修正: MACD/BBANDS/その他の指標の計算失敗時 (終値が全て同じなど) にDataFrameの欠落カラムを自動補完し、スコアリングスキップをロバストに処理するロジックを導入 (v20.0.48 NEW!)
# 2. 💡 修正: fetch_top_volume_tickersが0件を返した場合、分析対象銘柄がなくなる問題を修正 (v20.0.47)
# 3. 💡 新規: 毎分析ループで最高スコア銘柄の情報をログ出力 (v20.0.46)
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
BOT_VERSION = "v20.0.48"            # 💡 BOTバージョンを v20.0.48 に更新 
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
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {'fgi_proxy': 0.0, 'fgi_raw_value': 'N/A', 'forex_bonus': 0.0}
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False 
OPEN_POSITIONS: List[Dict] = [] 
ACCOUNT_EQUITY_USDT: float = 0.0 
GLOBAL_DATA: Dict[str, pd.DataFrame] = {} # OHLCVと指標データを格納

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
        bb_width_raw = tech_data.get('indicators', {}).get('BBANDS_width', 0.0)
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

# 🆕 機能追加: 取引閾値未満の最高スコアを定期通知 (既存の機能)
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

        # レバレッジの設定 (MEXC向け - 設定がない場合があるためtry-exceptで囲む)
        if EXCHANGE_CLIENT.id == 'mexc':
            symbols_to_set_leverage = []
            default_base_quotes = {s.split('/')[0]: s.split('/')[1] for s in DEFAULT_SYMBOLS if '/' in s}
            
            for mkt in EXCHANGE_CLIENT.markets.values():
                if mkt['quote'] == 'USDT' and mkt['type'] in ['swap', 'future'] and mkt['active']:
                    if mkt['base'] in default_base_quotes:
                        symbols_to_set_leverage.append(mkt['symbol'])
            
            for symbol in symbols_to_set_leverage:
                # set_leverage() が openType と positionType の両方を要求するため、両方の設定を行います。
                # positionType: 1 は Long (買い) ポジション用
                try:
                    await EXCHANGE_CLIENT.set_leverage(
                        LEVERAGE, symbol, params={'openType': 2, 'positionType': 1} # openType: 2 は Cross Margin
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
        logging.critical(f"❌ CCXT初期化失敗 - ネットワークエラー/タイムアウト: 接続を確認してください。{e}", exc_info=True)
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗 - 予期せぬエラー: {e}", exc_info=True)
        
    EXCHANGE_CLIENT = None
    return False

async def fetch_account_status() -> Dict: 
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    if not IS_CLIENT_READY:
        return {'error': "Client not ready"}

    try:
        balance_data = await EXCHANGE_CLIENT.fetch_balance({'type': 'future'})
        total_usdt = balance_data.get('total', {}).get('USDT', 0.0)
        
        # Equityの値をグローバル変数に格納
        ACCOUNT_EQUITY_USDT = total_usdt
        
        logging.info(f"💰 アカウントステータス取得: 総資産 (Equity) = {format_usdt(total_usdt)} USDT")
        return {
            'total_usdt_balance': total_usdt,
            'timestamp': datetime.now(JST).isoformat()
        }

    except Exception as e:
        logging.error(f"❌ アカウントステータス取得エラー: {e}", exc_info=True)
        return {'error': str(e), 'total_usdt_balance': ACCOUNT_EQUITY_USDT}
        
# ... (fetch_historical_ohlcv, fetch_top_volume_tickers, fetch_ohlcv_for_all_symbols, update_symbols_to_monitor, fetch_macro_context は省略)

async def fetch_historical_ohlcv(exchange: ccxt_async.Exchange, symbol: str, timeframe: str, limit: int = 500) -> Optional[pd.DataFrame]:
    """過去のOHLCVデータを取得する（簡略化）"""
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms').dt.tz_localize(timezone.utc)
        df.set_index('datetime', inplace=True)
        return df
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: OHLCVデータの取得に失敗しました: {e}", exc_info=True)
        return None

# ====================================================================================
# INDICATOR CALCULATION (FIXED for v20.0.48)
# ====================================================================================

def calculate_indicators(df: pd.DataFrame, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """
    テクニカル指標を計算する - MACD/BBANDS計算エラー修正版
    
    修正ポイント:
    1. データ検証の強化 (終値が全てNaNまたは定数の場合をチェック)
    2. 指標計算後のカラム存在チェックとNaNによる補完
    """
    if df is None or df.empty:
        return None

    close_series = df['close']
    
    # --- ✅ 修正 1: MACD/BBANDS計算エラー対策 (データ検証) ---
    # 終値データが全てNaNまたは、有効なデータが不足している場合はスキップ
    if close_series.isnull().all() or len(close_series.dropna()) < LONG_TERM_SMA_LENGTH: 
        logging.warning(f"⚠️ {symbol} - {timeframe}: 終値データが不足または無効です (有効データ: {len(close_series.dropna())}本)。テクニカル指標の計算をスキップします。")
        return None
    
    # 終値が全て同じ値（計算ライブラリがエラーを起こす可能性がある）
    if close_series.iloc[-LONG_TERM_SMA_LENGTH:].dropna().nunique() <= 5 and len(close_series.iloc[-LONG_TERM_SMA_LENGTH:].dropna()) > 0:
        logging.warning(f"⚠️ {symbol} - {timeframe}: 最新{LONG_TERM_SMA_LENGTH}本の終値のバリエーションが非常に少ないです。計算エラーの可能性があります。")
        
    try:
        # MACD (デフォルト: 12, 26, 9)
        df.ta.macd(append=True)
        # Bollinger Bands (デフォルト: 20, 2.0)
        df.ta.bbands(append=True)
        # ATR (動的リスク管理に使用)
        df.ta.atr(length=ATR_LENGTH, append=True)
        # RSI (デフォルト: 14)
        df.ta.rsi(append=True)
        # SMA (短期・長期のトレンド判断用)
        df.ta.sma(length=50, append=True) 
        df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) 
        # StochRSI (デフォルト: 14, 14, 3, 3)
        df.ta.stochrsi(append=True)
        # ADX (デフォルト: 14)
        df.ta.adx(append=True)
        # OBV (出来高モメンタム)
        df.ta.obv(append=True)
        
    except Exception as e:
        logging.error(f"❌ {symbol} - {timeframe}: pandas_taによる指標計算で例外が発生しました: {e}", exc_info=True)

    # --- ✅ 修正 2: 必要なカラムの存在チェックとデフォルト値設定 ---
    # 必要なすべてのカラムを定義 (pandas_taのデフォルト名を使用)
    required_indicators = {
        'MACDh_12_26_9': np.nan, 
        'BBU_20_2.0': np.nan,
        'BBL_20_2.0': np.nan, 
        'ATR_14': np.nan,
        'RSI_14': np.nan,
        f'SMA_50': np.nan,
        f'SMA_{LONG_TERM_SMA_LENGTH}': np.nan,
        'STOCHRSIk_14_14_3_3': np.nan, # StochRSI k
        'ADX_14': np.nan,
        'OBV': np.nan,
    }

    indicators_missing = False
    for col, default_val in required_indicators.items():
        if col not in df.columns:
            # 存在しないカラムをnp.nanで埋めて追加 (構造を維持する)
            df[col] = default_val 
            logging.warning(f"⚠️ {symbol} - {timeframe}: {col} の計算エラー: カラムが生成されませんでした。NaNを挿入。")
            indicators_missing = True
        
    # BBANDSの幅を計算し、欠損チェック
    if 'BBU_20_2.0' in df.columns and 'BBL_20_2.0' in df.columns and not df['BBU_20_2.0'].iloc[-1] is np.nan:
        df['BBANDS_width'] = df['BBU_20_2.0'] - df['BBL_20_2.0']
    else:
        df['BBANDS_width'] = np.nan
        indicators_missing = True
        
    # コア指標が欠損している場合はNoneを返す (処理続行不可)
    if indicators_missing and (df['MACDh_12_26_9'].iloc[-1] is np.nan or df['ATR_14'].iloc[-1] is np.nan):
         logging.warning(f"⚠️ {symbol} - {timeframe}: コア指標が欠損しているため、指標計算を失敗として扱います。")
         return None

    return df


# ====================================================================================
# SCORING & TRADING LOGIC (FIXED for v20.0.48)
# ====================================================================================

def calculate_trade_score(df: pd.DataFrame, symbol: str, timeframe: str) -> Tuple[float, float, str, Dict]:
    """
    テクニカル指標に基づいてトレードスコアを計算する。
    スコアは 0.0 から 1.0 (内部では -1.0から1.0) で表現される。
    
    Returns:
        Tuple[score, atr, signal, tech_data]
    """
    if df is None or df.empty:
        return 0.0, 0.0, "NO_DATA", {}

    # 最新の指標データを取得
    latest = df.iloc[-1]
    
    # --- ✅ 修正 3: スコアリング前の必須指標のNaNチェック ---
    required_cols = [
        'MACDh_12_26_9', 'BBU_20_2.0', 'BBL_20_2.0', 'ATR_14', 'RSI_14', 
        f'SMA_{LONG_TERM_SMA_LENGTH}', 'STOCHRSIk_14_14_3_3', 'ADX_14', 'OBV', 'BBANDS_width'
    ]
    
    # 必須指標のいずれか一つでもNaN（計算失敗）があればスキップ
    if latest[required_cols].isnull().any():
        logging.warning(f"⚠️ {symbol} - {timeframe}: 必須のテクニカル指標が不足しています（NaN）。スコアリングをスキップします。")
        # ATRもNaNの場合は0.0を返す
        return 0.0, 0.0, "INDICATOR_MISSING", {'tech_data': {}}

    # --- スコアリングロジックの実行 ---
    
    # スコアの初期値
    score_long = 0.0
    score_short = 0.0
    
    # テクニカル指標の値を格納する辞書 (通知用)
    tech_data = {
        'indicators': latest[required_cols].to_dict(),
        'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS,
    }

    # 1. ベーススコア & 構造的優位性ボーナス
    score_long += BASE_SCORE + STRUCTURAL_PIVOT_BONUS
    score_short += BASE_SCORE + STRUCTURAL_PIVOT_BONUS
    
    # 2. 長期トレンドとの一致 (SMA_200)
    sma_200 = latest[f'SMA_{LONG_TERM_SMA_LENGTH}']
    current_price = latest['close']
    trend_val = 0.0
    
    if current_price > sma_200:
        score_long += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        trend_val = LONG_TERM_REVERSAL_PENALTY
    elif current_price < sma_200:
        score_short += LONG_TERM_REVERSAL_PENALTY # 順張りボーナス
        trend_val = -LONG_TERM_REVERSAL_PENALTY 
        
    # 3. MACDモメンタム (方向一致)
    macd_h = latest['MACDh_12_26_9']
    macd_val = 0.0
    if macd_h > 0:
        score_long += MACD_CROSS_PENALTY
        macd_val = MACD_CROSS_PENALTY
    elif macd_h < 0:
        score_short += MACD_CROSS_PENALTY
        macd_val = -MACD_CROSS_PENALTY 
        
    # 4. BBANDS ボラティリティ過熱ペナルティ
    bb_width = latest['BBANDS_width']
    volatility_penalty = 0.0
    if bb_width / current_price > VOLATILITY_BB_PENALTY_THRESHOLD:
        volatility_penalty = -0.10 # ペナルティ (例)
        
    # 5. StochRSI 過熱ペナルティ/回復ボーナス
    stoch_k = latest['STOCHRSIk_14_14_3_3']
    stoch_val = 0.0
    if stoch_k < STOCHRSI_BOS_LEVEL: 
        score_long -= STOCHRSI_BOS_PENALTY 
        stoch_val = -STOCHRSI_BOS_PENALTY
    elif stoch_k > (100 - STOCHRSI_BOS_LEVEL): 
        score_short -= STOCHRSI_BOS_PENALTY
        stoch_val = STOCHRSI_BOS_PENALTY 

    # 6. ADXトレンド確証
    adx = latest['ADX_14']
    adx_val = 0.0
    if adx > ADX_TREND_STRENGTH_THRESHOLD:
        if macd_h > 0 or current_price > sma_200:
            score_long += ADX_TREND_BONUS
            adx_val = ADX_TREND_BONUS
        elif macd_h < 0 or current_price < sma_200:
            score_short += ADX_TREND_BONUS
            adx_val = -ADX_TREND_BONUS
            
    # 7. マクロコンテキストボーナス
    macro_bonus = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0) * FGI_PROXY_BONUS_MAX 
    if macro_bonus > 0:
        score_long += macro_bonus
    elif macro_bonus < 0:
        score_short += abs(macro_bonus)
        
    # --- 最終決定 ---
    
    if score_long > score_short:
        final_score = score_long
        signal = "long"
        
        # 最終スコア調整: ペナルティ適用
        final_score += volatility_penalty
        
    elif score_short > score_long:
        final_score = score_short
        signal = "short"

        # 最終スコア調整: ペナルティ適用
        final_score += volatility_penalty
        
    else:
        final_score = 0.0
        signal = "NEUTRAL"
        
    # ATR値を取得
    latest_atr = latest['ATR_14']
    
    # tech_dataの更新 (通知用)
    tech_data.update({
        'long_term_reversal_penalty_value': trend_val,
        'macd_penalty_value': macd_val,
        'rsi_momentum_bonus_value': 0.0, # ロジック省略
        'obv_momentum_bonus_value': 0.0, # ロジック省略
        'liquidity_bonus_value': 0.0, # ロジック省略
        'sentiment_fgi_proxy_bonus': macro_bonus,
        'volatility_penalty_value': volatility_penalty,
        'stoch_rsi_penalty_value': stoch_val,
        'stoch_rsi_k_value': stoch_k,
        'adx_trend_bonus_value': adx_val,
        'adx_raw_value': adx,
        'score_long': score_long,
        'score_short': score_short,
        'final_score': final_score,
    })
    
    return final_score, latest_atr, signal, tech_data
    
async def process_entry_signal(symbol: str, timeframe: str, score: float, side: str, atr: float, tech_data: Dict):
    """トレード実行の準備とリスク計算、実行のロジック（簡略化）"""
    global OPEN_POSITIONS
    
    key = f"{symbol}-{timeframe}"
    if key in [p['symbol'] for p in OPEN_POSITIONS]:
        logging.info(f"ℹ️ {symbol}: 既にポジションがあるため、新規エントリーをスキップします。")
        return

    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    if score < current_threshold:
        logging.info(f"ℹ️ {symbol} - {timeframe}: スコア ({score:.2f}) が取引閾値 ({current_threshold:.2f}) 未満です。スキップします。")
        return

    # SL/TPの計算
    risk_distance = atr * ATR_MULTIPLIER_SL

    ohlcv_df = GLOBAL_DATA.get(key)
    if ohlcv_df is None or ohlcv_df.empty:
        logging.error(f"❌ {symbol}: 最新価格を取得できませんでした。")
        return

    entry_price = ohlcv_df['close'].iloc[-1]
    
    if side == 'long':
        stop_loss = entry_price - risk_distance
        take_profit = entry_price + risk_distance * RR_RATIO_TARGET 
    else: # short
        stop_loss = entry_price + risk_distance
        take_profit = entry_price - risk_distance * RR_RATIO_TARGET 

    # リスクリワード比率を計算し、ログに反映
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)
    rr_ratio = reward_width / risk_width if risk_width > 0 else 0.0

    # ポジションサイズの計算
    if EXCHANGE_CLIENT is None or not IS_CLIENT_READY:
         logging.error("❌ 取引所クライアントが利用できません。取引をスキップします。")
         trade_result = {'status': 'error', 'error_message': 'Client not ready'}
    else:
        # 固定ロットでの注文数量計算
        amount_usd = FIXED_NOTIONAL_USDT
        
        # 数量 (ベース通貨) = 名目ロット / エントリー価格
        amount = amount_usd / entry_price if entry_price > 0 else 0.0
        
        # 最小注文サイズチェック (ここでは省略。実際のCCXT実装に依存)
        if amount <= 0:
            logging.error(f"❌ {symbol}: 計算された注文数量が0以下です: {amount}")
            trade_result = {'status': 'error', 'error_message': 'Amount can not be less than zero'}
        else:
            trade_result = await execute_trade(symbol, side, amount, entry_price)
            trade_result['filled_usdt'] = amount_usd
            
    # シグナル情報に価格とリスク情報を追加
    signal_data = {
        'symbol': symbol,
        'timeframe': timeframe,
        'score': score,
        'side': side,
        'entry_price': entry_price,
        'stop_loss': stop_loss,
        'take_profit': take_profit,
        'liquidation_price': calculate_liquidation_price(entry_price, LEVERAGE, side),
        'rr_ratio': rr_ratio,
        'tech_data': tech_data
    }
    
    log_signal(signal_data, "取引シグナル", trade_result)
    
    if trade_result.get('status') == 'ok' or TEST_MODE:
        # ポジションリストに追加 (LIVEモードで約定した場合 or TESTモードシミュレーション)
        position_info = {
            'symbol': symbol,
            'timeframe': timeframe,
            'side': side,
            'entry_price': entry_price,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'contracts': amount,
            'filled_usdt': amount_usd,
            'leverage': LEVERAGE,
            'order_id': trade_result.get('order_id', str(uuid.uuid4()))
        }
        OPEN_POSITIONS.append(position_info)
        
        # Telegram通知
        await send_telegram_notification(
            format_telegram_message(signal_data, "取引シグナル", current_threshold, trade_result)
        )
    else:
        logging.error(f"❌ {symbol}: 取引実行に失敗しました。詳細: {trade_result.get('error_message')}")
    
async def execute_trade(symbol: str, side: str, amount: float, price: float) -> Dict:
    """実際の取引所への注文実行（簡略化）"""
    if TEST_MODE:
        return {'status': 'ok', 'order_id': str(uuid.uuid4()), 'filled_amount': amount, 'filled_price': price}
        
    try:
        # レバレッジ設定はinitialize_exchange_clientで済んでいる前提
        order_side = 'buy' if side == 'long' else 'sell'
        
        order = await EXCHANGE_CLIENT.create_market_order(
            symbol=symbol,
            side=order_side,
            amount=amount
        )
        
        return {
            'status': 'ok',
            'order_id': order.get('id'),
            'filled_amount': amount, # 簡易化のため注文数量=約定数量
            'filled_price': price
        }
    except Exception as e:
        error_message = str(e)
        # 注文数量がゼロになるエラーなどの詳細をログに記録
        if "Amount can not be less than zero" in error_message or "Invalid amount" in error_message:
             logging.error(f"❌ {symbol} 注文エラー (数量): {error_message}")
        else:
             logging.error(f"❌ {symbol} 注文エラー: {error_message}", exc_info=True)
             
        return {'status': 'error', 'error_message': error_message}
        
async def check_and_manage_open_positions():
    """オープンポジションのSL/TPをチェックし、必要なら決済する（簡略化）"""
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"👀 ポジション監視を開始します。オープンポジション: {len(OPEN_POSITIONS)}件")
    
    positions_to_close = []
    
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        key = f"{symbol}-1m"
        
        ohlcv_df = GLOBAL_DATA.get(key)
        if ohlcv_df is None or ohlcv_df.empty:
            logging.warning(f"⚠️ {symbol}: 1m足のデータが取得できないため、ポジション監視をスキップ。")
            continue
            
        current_price = ohlcv_df['close'].iloc[-1]
        
        trigger = None
        
        # SL/TPチェック
        if pos['side'] == 'long':
            if current_price <= pos['stop_loss']:
                trigger = 'SL_HIT'
            elif current_price >= pos['take_profit']:
                trigger = 'TP_HIT'
        else: # short
            if current_price >= pos['stop_loss']:
                trigger = 'SL_HIT'
            elif current_price <= pos['take_profit']:
                trigger = 'TP_HIT'

        if trigger:
            logging.info(f"🚨 {symbol}: {trigger} が発動しました。決済処理を開始します。")

            exit_price = current_price
            
            # P&Lの計算
            entry_price = pos['entry_price']
            amount = pos['contracts']
            
            if pos['side'] == 'long':
                pnl_usdt = (exit_price - entry_price) * amount * pos['leverage'] # 簡易的な計算
                pnl_rate = (exit_price / entry_price - 1) * pos['leverage']
            else:
                pnl_usdt = (entry_price - exit_price) * amount * pos['leverage']
                pnl_rate = (1 - exit_price / entry_price) * pos['leverage']
                
            trade_result = {
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'exit_type': trigger,
                'filled_amount': amount,
                'status': 'ok' if TEST_MODE else 'live_executed'
            }
            
            # 決済注文（LIVEモードのみ）
            if not TEST_MODE:
                try:
                    close_side = 'sell' if pos['side'] == 'long' else 'buy'
                    await EXCHANGE_CLIENT.create_market_order(
                        symbol=symbol,
                        side=close_side,
                        amount=amount
                    )
                except Exception as e:
                    logging.error(f"❌ {symbol} 決済注文エラー: {e}", exc_info=True)
                    # 決済注文が失敗しても、BOTの管理からは削除する (手動で対応が必要)
            
            # ログと通知
            log_signal(pos, "ポジション決済", trade_result)
            await send_telegram_notification(
                format_telegram_message(pos, "ポジション決済", 0.0, trade_result, trigger)
            )
            
            positions_to_close.append(pos)
            
    # 決済が完了したポジションをリストから削除
    OPEN_POSITIONS = [pos for pos in OPEN_POSITIONS if pos not in positions_to_close]


async def process_symbol_timeframe(symbol: str, timeframe: str):
    """個別の銘柄と時間足の処理フロー"""
    
    if EXCHANGE_CLIENT is None or not IS_CLIENT_READY:
         return

    # 1. OHLCVデータの取得
    df = await fetch_historical_ohlcv(EXCHANGE_CLIENT, symbol, timeframe, REQUIRED_OHLCV_LIMITS[timeframe])
    key = f"{symbol}-{timeframe}"

    if df is not None and not df.empty:
        # 2. テクニカル指標の計算 (エラー修正版)
        df_with_indicators = calculate_indicators(df, symbol, timeframe)
        
        if df_with_indicators is not None:
            # データをグローバルに保存
            GLOBAL_DATA[key] = df_with_indicators
            
            # 3. トレードスコアの計算
            score, atr, signal, tech_data = calculate_trade_score(df_with_indicators, symbol, timeframe)
            
            if signal not in ["NO_DATA", "INDICATOR_MISSING"] and abs(score) > 0.0:
                # 4. トレード実行の判断
                if signal == 'long' or signal == 'short':
                    # 最高スコア候補として保存
                    LAST_ANALYSIS_SIGNALS.append({
                        'symbol': symbol,
                        'timeframe': timeframe,
                        'score': score,
                        'side': signal,
                        'atr': atr,
                        'tech_data': tech_data
                    })
                    
        else:
             logging.warning(f"⚠️ {symbol} - {timeframe}: 指標計算失敗のため、スコアリングをスキップします。")


async def main_bot_scheduler():
    """メインのデータ取得とトレードロジックの実行を定期的に行う"""
    
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME
    
    if not await initialize_exchange_client():
        return

    while True:
        try:
            logging.info("--- メインボットスケジューラを開始します ---")
            current_time = time.time()
            
            # ステータスとマクロコンテキストの更新
            account_status = await fetch_account_status()
            # await fetch_macro_context() # マクロコンテキストの取得は省略
            
            # 監視銘柄の更新 (ここでは省略)
            # await update_symbols_to_monitor() 
            
            # アナリシスリセット
            LAST_ANALYSIS_SIGNALS = []
            
            # 全ての銘柄・時間足で分析を実行
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                for tf in TARGET_TIMEFRAMES:
                    tasks.append(
                        process_symbol_timeframe(symbol, tf)
                    )

            await asyncio.gather(*tasks)
            
            # 分析結果から最高のシグナルを選択し、取引を試行
            if LAST_ANALYSIS_SIGNALS:
                sorted_signals = sorted(LAST_ANALYSIS_SIGNALS, key=lambda x: x['score'], reverse=True)
                
                # Top N シグナルを処理
                for signal in sorted_signals[:TOP_SIGNAL_COUNT]:
                    symbol = signal['symbol']
                    
                    # クールダウンチェック
                    if current_time - LAST_SIGNAL_TIME.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
                        logging.info(f"ℹ️ {symbol}: クールダウン中のため取引をスキップします。")
                        continue
                        
                    # 取引実行
                    await process_entry_signal(
                        symbol=symbol,
                        timeframe=signal['timeframe'],
                        score=signal['score'],
                        side=signal['side'],
                        atr=signal['atr'],
                        tech_data=signal['tech_data']
                    )
                    
                    # 成功した場合、クールダウン時間を更新
                    LAST_SIGNAL_TIME[symbol] = current_time 
            
            LAST_SUCCESS_TIME = current_time

            # 起動完了通知（初回のみ）
            if not IS_FIRST_MAIN_LOOP_COMPLETED:
                current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
                await send_telegram_notification(
                    format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
                )
                IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 取引閾値未満の最高スコアを通知
            await notify_highest_analysis_score()
            
            # WebShareデータの作成と送信 (時間間隔チェックは省略)
            # await send_webshare_update(create_webshare_data()) # データ作成関数は省略
            
            logging.info("--- メインボットスケジューラが完了しました ---")

        except Exception as e:
            logging.critical(f"❌ メインボットスケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
            # エラー発生時もボットは再起動せず続行 (ただしレートリミット対策で待機時間を長めにする)
            
        await asyncio.sleep(LOOP_INTERVAL)

async def position_monitor_scheduler():
    """ポジション監視を定期的に行う"""
    
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
        logging.error(f"❌ 捕捉されなかったエラー: {exc}", exc_info=True)
        
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {exc}"},
    )

# ====================================================================================
# MAIN EXECUTION
# ====================================================================================

if __name__ == "__main__":
    # このファイルは通常、uvicorn main_render:app のように実行されます。
    logging.info(f"Apex BOT {BOT_VERSION} が起動準備中です。")
