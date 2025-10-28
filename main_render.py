# ====================================================================================
# Apex BOT v20.0.43 - Future Trading / 30x Leverage 
# (Feature: 実践的スコアリングロジック、ATR動的リスク管理導入)
# 
# 🚨 致命的エラー修正強化: 
# 1. 💡 修正: np.polyfitの戻り値エラー (ValueError: not enough values to unpack) を修正
# 2. 💡 修正: fetch_tickersのAttributeError ('NoneType' object has no attribute 'keys') 対策 
# 3. 注文失敗エラー (Amount can not be less than zero) 対策
# 4. 💡 修正: 通知メッセージでEntry/SL/TP/清算価格が0になる問題を解決 (v20.0.42で対応済み)
# 5. 💡 新規: ダミーロジックを実践的スコアリングロジックに置換 (v20.0.43)
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
BOT_VERSION = "v20.0.43"            # 💡 BOTバージョンを更新 
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

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
REQUIRED_OHLCV_LIMITS = {'1m': 1000, '5m': 1000, '15m': 1000, '1h': 1000, '4h': 1000} 

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
RR_RATIO_TARGET = 1.5               # 💡 新規追加: 基本的なリスクリワード比率 (1:1.5)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.90       
SIGNAL_THRESHOLD_NORMAL = 0.85      
SIGNAL_THRESHOLD_ACTIVE = 0.80      

RSI_DIVERGENCE_BONUS = 0.10         
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # BB幅が価格の1%を超えるとペナルティ
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
                    'stop_loss': 0.0, # ボット内で動的に設定/管理
                    'take_profit': 0.0, # ボット内で動的に設定/管理
                    'leverage': p.get('leverage', LEVERAGE),
                    'raw_info': p, # デバッグ用
                })
        
        # グローバルポジションリストを更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        logging.info(f"✅ ポジション情報 ({len(open_positions)}件) を取得しました。")

        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': open_positions,
            'error': False
        }
    
    except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.DDoSProtection) as e:
        logging.error(f"❌ 口座情報取得中に接続エラーが発生しました: {e}")
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': str(e)}
    except Exception as e:
        logging.error(f"❌ 口座情報取得中に予期せぬエラーが発生しました: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': str(e)}

async def fetch_fgi_data() -> Dict:
    """
    Fear & Greed Index (FGI) データを取得し、マクロコンテキストを更新する。
    """
    global GLOBAL_MACRO_CONTEXT
    
    logging.info("⏳ FGI (恐怖・貪欲指数) を取得中...")
    
    try:
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        
        data = response.json()
        fgi_data = data['data'][0]
        
        value = int(fgi_data['value'])
        value_classification = fgi_data['value_classification']
        
        # FGI (0-100) を -0.05 から +0.05 の範囲に正規化し、マクロボーナスとして使用
        # 50(Neutral)を0.0として、0(Extreme Fear)を-0.05、100(Extreme Greed)を+0.05に対応させる
        fgi_proxy = ((value - 50) / 50) * FGI_PROXY_BONUS_MAX
        
        GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
        GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{value} ({value_classification})"
        
        logging.info(f"✅ FGIデータ取得完了: {value} ({value_classification}) -> Proxy: {fgi_proxy:+.4f}")
        return GLOBAL_MACRO_CONTEXT
        
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGIデータ取得中にエラーが発生しました: {e}")
        return GLOBAL_MACRO_CONTEXT
    except Exception as e:
        logging.error(f"❌ FGIデータ処理中に予期せぬエラーが発生しました: {e}", exc_info=True)
        return GLOBAL_MACRO_CONTEXT
        
# 💡 致命的エラー修正対象 2: AttributeError: 'NoneType' object has no attribute 'keys' 対策
async def fetch_top_volume_symbols(limit: int = TOP_SYMBOL_LIMIT) -> List[str]:
    """
    取引所のティッカー情報から、USDT建ての先物/スワップ市場で
    過去24時間の出来高に基づいたTOP銘柄を取得する。
    """
    global EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS
    
    if SKIP_MARKET_UPDATE:
        logging.warning("⚠️ SKIP_MARKET_UPDATEがTrueのため、出来高トップ銘柄の更新をスキップします。")
        return DEFAULT_SYMBOLS
        
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 銘柄リスト取得失敗: CCXTクライアントが準備できていません。デフォルトリストを返します。")
        return DEFAULT_SYMBOLS
        
    try:
        logging.info("⏳ 出来高トップ銘柄のリストを更新中...")
        
        # 出来高 (Volume) 情報を含むティッカーをまとめて取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        # 💡 修正: Noneが返された場合もここで早期リターン (AttributeError対策)
        if not tickers or not isinstance(tickers, dict):
            logging.error("❌ fetch_tickersが空または不正なデータ (None) を返しました。デフォルトリストを返します。")
            return DEFAULT_SYMBOLS
            
        # フィルタリングとランキング
        filtered_tickers: List[Dict] = []
        for symbol, ticker in tickers.items():
            
            # NoneType Error 対策 (念のため)
            if ticker is None or not isinstance(ticker, dict):
                 continue
                 
            # 1. USDT建ての先物/スワップ市場 (例: BTC/USDT) のみ
            if not symbol.endswith('/USDT'):
                continue
            
            # 2. CCXTのシンボル情報に市場タイプが 'swap' または 'future' であることを確認
            # ccxtのシンボル情報がロードされていることを前提とする
            market = EXCHANGE_CLIENT.markets.get(symbol)
            if not market or market.get('type') not in ['swap', 'future'] or market.get('active') is False:
                continue

            # 3. 24時間出来高 (quote volume) が存在すること
            quote_volume = ticker.get('quoteVolume') # USDT建てでの出来高
            if quote_volume is not None and quote_volume > 0:
                filtered_tickers.append({
                    'symbol': symbol,
                    'quoteVolume': quote_volume
                })
            
        # 出来高で降順にソート
        sorted_tickers = sorted(filtered_tickers, key=lambda x: x['quoteVolume'], reverse=True)
        
        # TOP Nのシンボルリストを作成
        top_symbols = [t['symbol'] for t in sorted_tickers[:limit]]
        
        # 出来高上位リストに、念のためデフォルトの主要銘柄を追加 (重複は自動で除去)
        final_symbols = list(set(top_symbols + DEFAULT_SYMBOLS))
        
        # グローバル変数に格納
        CURRENT_MONITOR_SYMBOLS = final_symbols
        
        logging.info(f"✅ 出来高上位 {limit} 銘柄を取得しました。合計監視銘柄数: {len(final_symbols)} (TOP {len(top_symbols)} + Default {len(DEFAULT_SYMBOLS)})")
        return final_symbols
        
    except ccxt.DDoSProtection as e:
        logging.error(f"❌ レートリミット違反: {e}。銘柄リスト更新をスキップ。")
        return DEFAULT_SYMBOLS
    except Exception as e:
        # 内部で発生した 'NoneType' object has no attribute 'keys' もここで捕捉され、デフォルトリストが返る
        logging.error(f"❌ 出来高トップ銘柄の取得中にエラーが発生しました: {e}", exc_info=True)
        return DEFAULT_SYMBOLS


async def fetch_ohlcv_data(symbol: str, timeframes: List[str]) -> Dict[str, pd.DataFrame]:
    """
    指定された複数の時間枠のOHLCVデータを取得し、Pandas DataFrameとして返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {}

    ohlcv_data: Dict[str, pd.DataFrame] = {}
    
    for tf in timeframes:
        try:
            limit = REQUIRED_OHLCV_LIMITS.get(tf, 500)
            
            # rate limit対策
            await asyncio.sleep(EXCHANGE_CLIENT.rateLimit / 1000) 
            
            # fetch_ohlcv() の結果は [timestamp, open, high, low, close, volume] のリスト
            ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, tf, limit=limit)
            
            if not ohlcv or len(ohlcv) < limit:
                logging.warning(f"⚠️ {symbol} {tf}: データ数が不足しています (取得数: {len(ohlcv)} / 必要数: {limit})。")
                
            if ohlcv:
                # DataFrameに変換し、CCXTが要求するカラム名を設定
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                # タイムスタンプをDatetimeIndexに変換
                df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
                df = df.set_index('timestamp')
                ohlcv_data[tf] = df
            
        except Exception as e:
            logging.error(f"❌ {symbol} {tf}: OHLCVデータ取得中にエラーが発生しました: {e}")
            continue
            
    return ohlcv_data

# ====================================================================================
# TRADING STRATEGY (SCORING)
# ====================================================================================

# ------------------------------------------------------------------------------------------------
# 6. スコア計算ロジック
# ------------------------------------------------------------------------------------------------

def calculate_signal_score(ohlcv_data: Dict[str, pd.DataFrame], symbol: str, macro_context: Dict, latest_price: float) -> Optional[Dict]:
    """
    OHLCVデータとマクロ環境に基づき、ロング・ショートの総合スコアを計算する。
    
    スコア構成 (合計 1.0)
    - ベース構造 (0.40)
    - 長期トレンド一致/逆行 (-0.20 ~ +0.20)
    - MACDモメンタム一致/逆行 (-0.15 ~ +0.15)
    - RSIモメンタム加速 (0.00 ~ +0.10)
    - OBV出来高確証 (0.00 ~ +0.04)
    - ボラティリティペナルティ (-0.10 ~ 0.00)
    - 流動性ボーナス (0.00 ~ +0.06)
    - マクロ環境影響 (-0.05 ~ +0.05)
    -----------------------------------------------------
    合計: 0.25 (最悪) ~ 1.00 (最高)
    """
    
    if not ohlcv_data or '1h' not in ohlcv_data or '4h' not in ohlcv_data:
        # 必要な時間足データがない場合はスキップ
        return None 
        
    tech_data: Dict[str, Any] = {}
    
    # スコア計算の起点 (0.40)
    base_score = BASE_SCORE 
    
    # ----------------------------------------
    # 6.1 長期トレンド: 4h足のSMA200からの乖離方向
    # ----------------------------------------
    try:
        df_4h = ohlcv_data['4h']
        # 4h足の終値が十分に存在するか確認
        if len(df_4h) < LONG_TERM_SMA_LENGTH:
            # データ不足の場合、ペナルティを適用しない
            trend_penalty = 0.0
        else:
            # SMA200を計算
            df_4h['SMA200'] = ta.sma(df_4h['close'], length=LONG_TERM_SMA_LENGTH)
            latest_close_4h = df_4h['close'].iloc[-1]
            latest_sma_200 = df_4h['SMA200'].iloc[-1]
            
            # SMA200に対する現在の価格の相対位置に基づいてペナルティ/ボーナスを設定
            if latest_close_4h > latest_sma_200:
                # 価格がSMA200の上にある = ロングに順張りボーナス
                trend_penalty = LONG_TERM_REVERSAL_PENALTY 
            elif latest_close_4h < latest_sma_200:
                # 価格がSMA200の下にある = ショートに順張りボーナス (ロングにはペナルティ)
                trend_penalty = -LONG_TERM_REVERSAL_PENALTY 
            else:
                trend_penalty = 0.0 # 中立
                
        tech_data['long_term_reversal_penalty_value'] = trend_penalty
        
    except Exception as e:
        logging.error(f"❌ {symbol}: 長期トレンド分析エラー: {e}")
        tech_data['long_term_reversal_penalty_value'] = 0.0

    
    # ----------------------------------------
    # 6.2 モメンタム: 1h足のMACDの方向
    # ----------------------------------------
    try:
        df_1h = ohlcv_data['1h']
        macd_result = ta.macd(df_1h['close'], fast=12, slow=26, signal=9, append=False)
        
        # MACD, MACDh (ヒストグラム) の値が十分に存在するか確認
        if macd_result is None or len(macd_result) < 20:
             macd_penalty = 0.0
        else:
            macd_h = macd_result.iloc[-1]['MACDh_12_26_9']
            macd_val = macd_result.iloc[-1]['MACD_12_26_9']
            
            # MACDヒストグラムがゼロラインより上 (上昇モメンタム)
            if macd_h > 0 and macd_val > 0:
                # ロングにモメンタムボーナス
                macd_penalty = MACD_CROSS_PENALTY
            # MACDヒストグラムがゼロラインより下 (下降モメンタム)
            elif macd_h < 0 and macd_val < 0:
                # ショートにモメンタムボーナス (ロングにはペナルティ)
                macd_penalty = -MACD_CROSS_PENALTY
            else:
                macd_penalty = 0.0 # MACDクロス近辺、または方向性不明
                
        tech_data['macd_penalty_value'] = macd_penalty
        
    except Exception as e:
        logging.error(f"❌ {symbol}: MACD分析エラー: {e}")
        tech_data['macd_penalty_value'] = 0.0

    
    # ----------------------------------------
    # 6.3 モメンタム加速: 15m足のRSIの適正水準
    # ----------------------------------------
    try:
        df_15m = ohlcv_data['15m']
        rsi_15m = ta.rsi(df_15m['close'], length=14, append=False).iloc[-1]
        rsi_bonus = 0.0
        
        if not math.isnan(rsi_15m):
            # RSI 40-60レンジ外をモメンタム加速と見なす
            if rsi_15m >= 60:
                # ロング優位: 60-70の間でボーナスを線形に増加
                rsi_bonus = min(RSI_DIVERGENCE_BONUS, (rsi_15m - 60) * (RSI_DIVERGENCE_BONUS / 10))
            elif rsi_15m <= 40:
                # ショート優位: 40-30の間でボーナスを線形に増加
                rsi_bonus = -min(RSI_DIVERGENCE_BONUS, (40 - rsi_15m) * (RSI_DIVERGENCE_BONUS / 10))
            
        tech_data['rsi_momentum_bonus_value'] = rsi_bonus
        
    except Exception as e:
        logging.error(f"❌ {symbol}: RSI分析エラー: {e}")
        tech_data['rsi_momentum_bonus_value'] = 0.0
        
    
    # ----------------------------------------
    # 6.4 ボラティリティ: 5m足のボリンジャーバンドの幅 (過熱ペナルティ)
    # ----------------------------------------
    try:
        df_5m = ohlcv_data['5m']
        bb_result = ta.bbands(df_5m['close'], length=20, std=2, append=False)
        
        if bb_result is None or len(bb_result) == 0:
            volatility_penalty = 0.0
        else:
            bb_upper = bb_result.iloc[-1][f'BBU_20_2.0']
            bb_lower = bb_result.iloc[-1][f'BBL_20_2.0']
            bb_width = bb_upper - bb_lower
            
            # 価格に対するBB幅の比率 (価格が0でないことを確認)
            price_relative_bb_width = bb_width / latest_price if latest_price > 0 else 0.0
            
            volatility_penalty = 0.0
            # BB幅が価格の1% (0.01) を超える場合にペナルティを適用
            if price_relative_bb_width > VOLATILITY_BB_PENALTY_THRESHOLD:
                # 0.01からの乖離に応じて最大-0.10までペナルティ
                penalty_factor = min(1.0, (price_relative_bb_width - VOLATILITY_BB_PENALTY_THRESHOLD) / VOLATILITY_BB_PENALTY_THRESHOLD)
                volatility_penalty = -0.10 * penalty_factor
                
        tech_data['volatility_penalty_value'] = volatility_penalty
        
    except Exception as e:
        logging.error(f"❌ {symbol}: ボラティリティ分析エラー: {e}")
        tech_data['volatility_penalty_value'] = 0.0
        
    
    # ----------------------------------------
    # 6.5 流動性ボーナス: 監視リストのTOPにあるほど流動性が高いと見なす
    # ----------------------------------------
    try:
        # 現在の監視銘柄リストにおけるインデックスを取得
        # DEFAULT_SYMBOLSをベースに、TOP_SYMBOL_LIMITまでの銘柄にボーナス
        
        # 出来高TOP銘柄リスト (CURRENT_MONITOR_SYMBOLSの先頭)
        rank = -1
        try:
             rank = CURRENT_MONITOR_SYMBOLS.index(symbol) # 0-indexed
        except ValueError:
            rank = -1
            
        liquidity_bonus = 0.0
        if 0 <= rank < TOP_SYMBOL_LIMIT:
            # 1位 (rank=0) で最大、TOP_SYMBOL_LIMIT位で0に収束
            # (TOP_SYMBOL_LIMIT - rank) / TOP_SYMBOL_LIMIT で係数を計算
            # 例: TOP_SYMBOL_LIMIT=40, rank=0 -> 1.0, rank=39 -> 1/40 = 0.025
            factor = (TOP_SYMBOL_LIMIT - rank) / TOP_SYMBOL_LIMIT 
            liquidity_bonus = LIQUIDITY_BONUS_MAX * factor
        
        tech_data['liquidity_bonus_value'] = liquidity_bonus
        
    except Exception as e:
        logging.error(f"❌ {symbol}: 流動性ボーナス計算エラー: {e}")
        tech_data['liquidity_bonus_value'] = 0.0

    
    # ----------------------------------------
    # 6.6 モメンタム確証: OBV (On-Balance Volume)
    # ----------------------------------------
    try:
        # 1hのOBVを使用
        obv_df = ohlcv_data['1h'].ta.obv(append=False).dropna() 
        
        if len(obv_df) < 50:
            tech_data['obv_momentum_bonus_value'] = 0.0
        else:
            # OBVの50期間の最小二乗法による傾き
            obv_recent = obv_df.iloc[-50:]
            x = np.arange(len(obv_recent))
            # 💡 致命的エラー修正 1: full=False -> full=True に変更
            slope, _, _, _, _ = np.polyfit(x, obv_recent.values, 1, full=True) 

            # 傾きがプラスでロング、マイナスでショートに傾斜ボーナス
            # slopeが0でないことを確認
            if abs(slope) > 0.0001:
                
                obv_momentum_bonus = 0.0
                # 傾きの絶対値が大きいほど確信度が高いと見なす
                # 傾きを正規化する: 過去の平均的な傾きを基準とするのは難しいので、固定のしきい値で判定
                
                # 傾きの絶対値が0.001以上の場合にボーナスを付与（具体的なしきい値は要調整）
                threshold_for_bonus = 0.001 
                
                if slope > threshold_for_bonus:
                    obv_momentum_bonus = OBV_MOMENTUM_BONUS
                elif slope < -threshold_for_bonus:
                    obv_momentum_bonus = -OBV_MOMENTUM_BONUS
                    
                tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus
            else:
                tech_data['obv_momentum_bonus_value'] = 0.0
        
    except Exception as e:
        logging.error(f"❌ {symbol}: OBV分析エラー: {e}", exc_info=True)
        tech_data['obv_momentum_bonus_value'] = 0.0

    
    # ----------------------------------------
    # 6.7 総合スコア計算
    # ----------------------------------------
    
    # 初期スコア
    long_score = base_score + STRUCTURAL_PIVOT_BONUS
    short_score = base_score + STRUCTURAL_PIVOT_BONUS
    
    # 1. 長期トレンド: ロングの場合はプラス、ショートの場合はマイナスを乗じる
    long_score += tech_data['long_term_reversal_penalty_value']
    short_score -= tech_data['long_term_reversal_penalty_value'] # 反対側に乗算
    
    # 2. MACDモメンタム
    long_score += tech_data['macd_penalty_value']
    short_score -= tech_data['macd_penalty_value'] # 反対側に乗算
    
    # 3. RSIモメンタム (RSIボーナスがプラスならロングに、マイナスならショートにボーナス)
    if tech_data['rsi_momentum_bonus_value'] > 0:
        long_score += tech_data['rsi_momentum_bonus_value']
    else:
        short_score += abs(tech_data['rsi_momentum_bonus_value'])
        
    # 4. OBV確証 (OBVボーナスがプラスならロングに、マイナスならショートにボーナス)
    if tech_data['obv_momentum_bonus_value'] > 0:
        long_score += tech_data['obv_momentum_bonus_value']
    else:
        short_score += abs(tech_data['obv_momentum_bonus_value'])

    # 5. ボラティリティペナルティ (両方に適用されるが、過熱時はペナルティ)
    # このペナルティはネガティブ値なので、そのまま加算する
    long_score += tech_data['volatility_penalty_value']
    short_score += tech_data['volatility_penalty_value']
    
    # 6. 流動性ボーナス (両方に適用)
    long_score += tech_data['liquidity_bonus_value']
    short_score += tech_data['liquidity_bonus_value']
    
    # 7. マクロ環境影響 (FGI Proxy - ロングに適用し、ショートには逆側を適用)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # マクロ影響値 (最大 FGI_PROXY_BONUS_MAX)
    sentiment_fgi_proxy_bonus = fgi_proxy
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus
    
    long_score += sentiment_fgi_proxy_bonus
    short_score -= sentiment_fgi_proxy_bonus # ショートは逆のセンチメントでペナルティ/ボーナス
    
    # スコアの正規化 (0.0 - 1.0) 
    # 最低値: 0.25 (ベース0.4 + 構造0.05 - トレンド0.2 - MACD0.15 - ボラ0.10 - FGI0.05)
    # 最大値: 1.00 (ベース0.4 + 構造0.05 + トレンド0.2 + MACD0.15 + RSI0.1 + OBV0.04 + 流動0.06 + FGI0.05)
    
    # 0.0以下の場合は強制的に0.0に
    long_score = max(0.0, long_score)
    short_score = max(0.0, short_score)
    
    # ----------------------------------------
    # 6.8 最終シグナル決定
    # ----------------------------------------
    
    final_signal: Optional[Dict] = None
    
    if long_score >= short_score and long_score >= get_current_threshold(macro_context):
        final_signal = {
            'symbol': symbol,
            'side': 'long',
            'score': long_score,
            'timeframe': '1h', # メイン分析足
            'latest_price': latest_price,
            'tech_data': tech_data
        }
    elif short_score > long_score and short_score >= get_current_threshold(macro_context):
        final_signal = {
            'symbol': symbol,
            'side': 'short',
            'score': short_score,
            'timeframe': '1h', # メイン分析足
            'latest_price': latest_price,
            'tech_data': tech_data
        }
        
    return final_signal

# ====================================================================================
# TRADING EXECUTION & RISK MANAGEMENT
# ====================================================================================

async def process_entry_signal(signal: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    シグナルに基づき、リスク管理を行い、エントリー注文を執行する。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT
    
    symbol = signal['symbol']
    side = signal['side']
    latest_price = signal['latest_price']
    
    # 1. リスクパラメータ計算 (ATRベースのSLとTP)
    try:
        # 5m足のATRを取得してボラティリティを推定
        df_5m = EXCHANGE_CLIENT.ohlcv_data.get(symbol, {}).get('5m')
        if df_5m is None or len(df_5m) < ATR_LENGTH:
            raise Exception("5m足のOHLCVデータが不足しており、ATR計算ができません。")
            
        # ATRを計算
        df_5m['ATR'] = ta.atr(df_5m['high'], df_5m['low'], df_5m['close'], length=ATR_LENGTH)
        latest_atr = df_5m['ATR'].iloc[-1]
        
        if latest_atr <= 0:
            raise Exception("ATRが0以下です。ボラティリティが極端に低いか計算エラーです。")
            
        # SLレベルを計算: ATRのN倍
        risk_level_price = latest_atr * ATR_MULTIPLIER_SL
        
        # 最低リスク幅チェック (総資産のMIN_RISK_PERCENT * レバレッジ)
        # これは「証拠金」に対するリスクではなく、「価格変動率」としての最低リスク幅
        min_risk_price = latest_price * MIN_RISK_PERCENT 
        
        if risk_level_price < min_risk_price:
             # 最低リスク幅を下回る場合、最低値に引き上げる (リスクが高すぎるのを防ぐため)
             risk_level_price = min_risk_price
             logging.warning(f"⚠️ {symbol}: ATRベースのSL幅が最低リスク幅({MIN_RISK_PERCENT*100:.2f}%)より小さいため、最低値に引き上げました。")

        # SL/TP価格の決定
        if side == 'long':
            stop_loss = latest_price - risk_level_price
            take_profit = latest_price + (risk_level_price * RR_RATIO_TARGET)
            
            # SLがエントリー価格の50%以下にならないようにチェック (極端な設定を防ぐ)
            if stop_loss < latest_price * 0.5:
                stop_loss = latest_price * 0.5
            
        else: # short
            stop_loss = latest_price + risk_level_price
            take_profit = latest_price - (risk_level_price * RR_RATIO_TARGET)
            
            # SLがエントリー価格の200%以上にならないようにチェック
            if stop_loss > latest_price * 2.0:
                 stop_loss = latest_price * 2.0

        # SL/TPをシグナルに格納
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        signal['entry_price'] = latest_price
        
        # リスクリワード比率を更新
        risk_width = abs(latest_price - stop_loss)
        reward_width = abs(take_profit - latest_price)
        rr_ratio = reward_width / risk_width if risk_width > 0 else 0.0
        signal['rr_ratio'] = rr_ratio
        
        # 清算価格の推定
        liquidation_price = calculate_liquidation_price(latest_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
        signal['liquidation_price'] = liquidation_price
        
        # ログに記録
        logging.info(f"✅ {symbol} ({side.capitalize()}): リスクパラメータ計算完了。SL: {format_price(stop_loss)}, TP: {format_price(take_profit)}, RRR: 1:{rr_ratio:.2f}")

    except Exception as e:
        logging.error(f"❌ {symbol}: リスクパラメータ計算中にエラーが発生しました: {e}", exc_info=True)
        # エラーが発生した場合、取引を中止する
        return None, {'status': 'error', 'error_message': f"リスク計算エラー: {e}"}

    # 2. ポジション量の計算 (固定ロット)
    # 固定の名目価値 (Notional Value) を使用
    notional_usdt = FIXED_NOTIONAL_USDT 
    
    # 注文数量 (Contracts) の計算
    try:
        # amount = notional_usdt * leverage / entry_price (MEXCはcontracts/amount=USDT数量ではない)
        # amount = notional_usdt / entry_price
        amount = notional_usdt / latest_price
        
        # MEXCは最小注文数量の制限が厳しいため、CCXTの市場情報を使用して調整する
        market = EXCHANGE_CLIENT.market(symbol)
        
        # amountを丸める
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        amount = float(amount)
        
        if amount <= 0:
            raise Exception("計算された注文数量が0以下です。")
            
        logging.info(f"ℹ️ {symbol}: 注文数量を {format_usdt(notional_usdt)} USDT (約 {amount:.4f} 契約) で設定します。")
        
    except Exception as e:
        logging.error(f"❌ {symbol}: 注文数量計算中にエラーが発生しました: {e}", exc_info=True)
        return None, {'status': 'error', 'error_message': f"注文数量計算エラー: {e}"}

    # 3. 注文の執行
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} {side.capitalize()} 注文をスキップしました。")
        # テスト結果を返す (ダミーの約定データ)
        trade_result = {
            'status': 'ok',
            'filled_amount': amount,
            'filled_usdt': notional_usdt,
            'entry_price': latest_price,
            'order_id': f'TEST-{uuid.uuid4()}',
            'timestamp': int(time.time() * 1000)
        }
        # ポジションリストにダミーポジションを追加
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'side': side,
            'contracts': amount,
            'entry_price': latest_price,
            'filled_usdt': notional_usdt,
            'liquidation_price': liquidation_price,
            'timestamp': trade_result['timestamp'],
            'stop_loss': stop_loss, 
            'take_profit': take_profit,
            'leverage': LEVERAGE,
        })
        return signal, trade_result

    # リアル注文の実行
    order_type = 'market'
    params = {}
    
    try:
        # 1. 成行注文でエントリー
        if side == 'long':
            order = await EXCHANGE_CLIENT.create_order(symbol, order_type, 'buy', amount, params=params)
        else:
            order = await EXCHANGE_CLIENT.create_order(symbol, order_type, 'sell', amount, params=params)
            
        # 注文の約定確認 (CCXTの成行注文は通常、即時約定/部分約定の情報を含む)
        # orderには 'filled' (約定数量), 'price' (約定価格), 'status' (closed/open) などが含まれる
        
        filled_amount = float(order.get('filled', 0.0))
        filled_price = float(order.get('price', latest_price))
        filled_usdt_notional = filled_amount * filled_price 

        if filled_amount > 0:
            trade_result = {
                'status': 'ok',
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt_notional,
                'entry_price': filled_price,
                'order_id': order.get('id'),
                'timestamp': order.get('timestamp')
            }
            logging.info(f"✅ {symbol} {side.capitalize()}: 成行注文が約定しました。数量: {filled_amount:.4f}、価格: {format_price(filled_price)}")
            
            # 💡 重要な更新: 約定価格、数量に基づき、SL/TP/清算価格を再計算
            
            # SL/TP価格の調整 (約定価格を使用)
            # ATR幅は変わらないものとして計算
            risk_level_price = abs(latest_price - stop_loss) # ATRベースの価格リスク幅を再利用
            
            if side == 'long':
                adjusted_stop_loss = filled_price - risk_level_price
                adjusted_take_profit = filled_price + (risk_level_price * RR_RATIO_TARGET)
            else: # short
                adjusted_stop_loss = filled_price + risk_level_price
                adjusted_take_profit = filled_price - (risk_level_price * RR_RATIO_TARGET)
            
            # 清算価格の再計算
            adjusted_liquidation_price = calculate_liquidation_price(filled_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)
            
            # シグナルとポジションリストを更新
            signal['stop_loss'] = adjusted_stop_loss
            signal['take_profit'] = adjusted_take_profit
            signal['entry_price'] = filled_price # 実際に約定した価格
            signal['liquidation_price'] = adjusted_liquidation_price
            
            # 2. ポジションリストに登録
            OPEN_POSITIONS.append({
                'symbol': symbol,
                'side': side,
                'contracts': filled_amount,
                'entry_price': filled_price,
                'filled_usdt': filled_usdt_notional,
                'liquidation_price': adjusted_liquidation_price,
                'timestamp': trade_result['timestamp'],
                'stop_loss': adjusted_stop_loss, 
                'take_profit': adjusted_take_profit,
                'leverage': LEVERAGE,
                'original_signal': signal, # 参照用
            })
            
            return signal, trade_result
        else:
            # 約定数量が0の場合 (通常、成行注文では稀)
            error_message = f"成行注文が約定しませんでした。ステータス: {order.get('status')}"
            logging.error(f"❌ {symbol}: {error_message}")
            return None, {'status': 'error', 'error_message': error_message}

    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ {symbol}: エントリー注文失敗 - {error_message}", exc_info=True)
        return None, {'status': 'error', 'error_message': error_message}
    except Exception as e:
        error_message = f"予期せぬエラー: {e}"
        logging.error(f"❌ {symbol}: エントリー注文失敗 - {error_message}", exc_info=True)
        return None, {'status': 'error', 'error_message': error_message}

async def close_position(position: Dict, exit_type: str) -> Optional[Dict]:
    """
    指定されたポジションを成行注文で決済する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts = abs(position['contracts'])
    entry_price = position['entry_price']
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: {symbol} {side.capitalize()} ポジション決済をスキップしました。({exit_type})")
        
        # ダミーの決済価格 (SL/TP価格のどちらか、または最新価格)
        exit_price = 0.0
        if exit_type == 'SL_TRIGGER':
            exit_price = position.get('stop_loss', position['entry_price'] * 0.9)
        elif exit_type == 'TP_TRIGGER':
            exit_price = position.get('take_profit', position['entry_price'] * 1.1)
        else:
            # 最新価格を取得 (ダミー)
            exit_price = position['entry_price'] * (1 + random.uniform(-0.005, 0.005))
            
        pnl_rate = ((exit_price - entry_price) / entry_price) * LEVERAGE * (1 if side == 'long' else -1)
        pnl_usdt = position['filled_usdt'] * pnl_rate 
        
        trade_result = {
            'status': 'ok',
            'exit_type': exit_type,
            'filled_amount': contracts,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'timestamp': int(time.time() * 1000)
        }
        
        # ポジションリストから削除
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position]
        
        return trade_result

    # リアル注文の実行
    order_type = 'market'
    
    try:
        # 決済注文のタイプを決定: ロングは売り、ショートは買い
        close_side = 'sell' if side == 'long' else 'buy'
        
        # 注文執行
        order = await EXCHANGE_CLIENT.create_order(symbol, order_type, close_side, contracts)
        
        # 注文の約定確認
        filled_amount = float(order.get('filled', 0.0))
        exit_price = float(order.get('price', 0.0)) # 約定価格
        
        if filled_amount > 0 and order.get('status') == 'closed':
            
            # PNLの計算 (MEXC APIがポジションを返さなくなるため、CCXTの標準PNL計算が使用できない場合を想定)
            # PNL = (ExitPrice - EntryPrice) * Contracts * SideMulti (Long: 1, Short: -1)
            side_multiplier = 1 if side == 'long' else -1
            pnl_usdt = (exit_price - entry_price) * contracts * side_multiplier
            
            # PNL率の計算 (レバレッジを考慮したROI)
            # PNL Rate = PNL / Initial Margin = PNL / (Notional Value / Leverage)
            initial_margin = position['filled_usdt'] / LEVERAGE 
            pnl_rate = pnl_usdt / initial_margin if initial_margin > 0 else 0.0
            
            trade_result = {
                'status': 'ok',
                'exit_type': exit_type,
                'filled_amount': filled_amount,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl_usdt': pnl_usdt,
                'pnl_rate': pnl_rate,
                'timestamp': order.get('timestamp')
            }
            logging.info(f"✅ {symbol} {side.capitalize()}: ポジション決済完了。Exit Type: {exit_type}, PNL: {format_usdt(pnl_usdt)} USDT ({pnl_rate*100:.2f}%)")
            
            # ポジションリストから削除
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position]
            
            return trade_result
        else:
            error_message = f"決済注文が完全には約定しませんでした。ステータス: {order.get('status')}"
            logging.error(f"❌ {symbol}: ポジション決済失敗 - {error_message}")
            return {'status': 'error', 'error_message': error_message}

    except ccxt.ExchangeError as e:
        error_message = f"取引所エラー: {e}"
        logging.error(f"❌ {symbol}: ポジション決済失敗 - {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}
    except Exception as e:
        error_message = f"予期せぬエラー: {e}"
        logging.error(f"❌ {symbol}: ポジション決済失敗 - {error_message}", exc_info=True)
        return {'status': 'error', 'error_message': error_message}


# ====================================================================================
# SCHEDULERS & MAIN LOOPS
# ====================================================================================

async def position_monitor_and_update_sltp():
    """
    オープンポジションを監視し、SL/TPのトリガーをチェックする。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        return
        
    logging.info(f"⏳ ポジション監視中... ({len(OPEN_POSITIONS)}件)")

    # 全ポジションの最新価格を取得
    symbols_to_check = [p['symbol'] for p in OPEN_POSITIONS]
    
    # リアルタイムティッカーを取得
    try:
        # MEXCはfetch_tickers()を頻繁に実行するとレートリミットにかかりやすいため、
        # fetch_ticker()を個別に使用するか、レートリミットを考慮してまとめて取得する
        await asyncio.sleep(EXCHANGE_CLIENT.rateLimit / 1000)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_check)
        
    except Exception as e:
        logging.error(f"❌ ポジション監視中のティッカー取得エラー: {e}")
        return

    # ポジションのチェックと決済処理
    positions_to_close: List[Tuple[Dict, str]] = [] # (position, exit_type)
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        sl = position.get('stop_loss', 0.0)
        tp = position.get('take_profit', 0.0)
        
        # 最新の価格情報を取得
        ticker = tickers.get(symbol)
        if not ticker:
            logging.warning(f"⚠️ {symbol} の最新価格が取得できませんでした。スキップします。")
            continue
            
        latest_price = ticker.get('last')
        if latest_price is None:
            continue
        
        # SL/TPのトリガーチェック (成行決済なので、last priceで判定)
        
        # ロングの場合 (SL: 価格下落, TP: 価格上昇)
        if side == 'long':
            # SLトリガー
            if sl > 0 and latest_price <= sl:
                positions_to_close.append((position, 'SL_TRIGGER'))
                continue
            # TPトリガー
            if tp > 0 and latest_price >= tp:
                positions_to_close.append((position, 'TP_TRIGGER'))
                continue
                
        # ショートの場合 (SL: 価格上昇, TP: 価格下落)
        elif side == 'short':
            # SLトリガー
            if sl > 0 and latest_price >= sl:
                positions_to_close.append((position, 'SL_TRIGGER'))
                continue
            # TPトリガー
            if tp > 0 and latest_price <= tp:
                positions_to_close.append((position, 'TP_TRIGGER'))
                continue
                
        # 💡 清算価格のチェック (念のため)
        liquidation_price = position.get('liquidation_price', 0.0)
        if liquidation_price > 0:
            if side == 'long' and latest_price <= liquidation_price * 1.005: # 清算価格の少し手前
                logging.warning(f"🚨 {symbol} {side.capitalize()}: 価格が清算価格 ({format_price(liquidation_price)}) に近づいています。")
            if side == 'short' and latest_price >= liquidation_price * 0.995: # 清算価格の少し手前
                logging.warning(f"🚨 {symbol} {side.capitalize()}: 価格が清算価格 ({format_price(liquidation_price)}) に近づいています。")


    # 決済が必要なポジションを処理
    for position, exit_type in positions_to_close:
        logging.critical(f"🔥 {position['symbol']} {position['side'].capitalize()}: {exit_type} がトリガーされました。ポジションを決済します。")
        
        trade_result = await close_position(position, exit_type)
        
        if trade_result and trade_result.get('status') == 'ok':
             # 決済成功を通知
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT) # 通知用に取得
            message = format_telegram_message(position, "ポジション決済", current_threshold, trade_result, exit_type)
            await send_telegram_notification(message)
            # PNLログを記録
            log_signal(position, f"Trade Exit ({exit_type.replace('_TRIGGER', '')})", trade_result)
        else:
            # 決済失敗を通知 (再試行は次の監視ループに委ねる)
             logging.error(f"❌ {position['symbol']}: 決済処理に失敗しました。")
             await send_telegram_notification(f"🚨 <b>ポジション決済失敗通知</b>\n{position['symbol']} ({position['side'].capitalize()}): 決済リクエストに失敗しました。次回ループで再試行されます。")


async def run_main_loop():
    """
    データ取得、分析、シグナル生成、取引実行のメインロジック。
    """
    global IS_FIRST_MAIN_LOOP_COMPLETED, EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME
    
    logging.info("🚀 メイン分析ループを開始します...")
    
    # 1. 口座ステータスの取得
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.error("❌ 口座ステータス取得エラーのため、取引をスキップします。")
        return
        
    # 2. マクロ環境データの取得 (FGI)
    macro_context = await fetch_fgi_data()
    
    # 3. 出来高トップ銘柄のリスト更新
    # 💡 致命的エラー修正 2 が適用された関数を使用
    monitoring_symbols = await fetch_top_volume_symbols()
    
    if not monitoring_symbols:
        logging.error("❌ 監視銘柄リストが空のため、分析をスキップします。")
        return

    # 4. OHLCVデータの一括取得
    # 監視銘柄ごとに、設定された時間足のデータを並列で取得
    
    # 全シンボルのOHLCV取得タスクを生成
    ohlcv_tasks = [fetch_ohlcv_data(symbol, TARGET_TIMEFRAMES) for symbol in monitoring_symbols]
    ohlcv_results = await asyncio.gather(*ohlcv_tasks)
    
    # シンボル名と結果を対応させる
    symbol_ohlcv_map = {symbol: result for symbol, result in zip(monitoring_symbols, ohlcv_results)}
    
    # 5. 取引シグナル生成 (スコアリング)
    potential_signals: List[Dict] = []
    
    for symbol, ohlcv_data in symbol_ohlcv_map.items():
        if not ohlcv_data:
            continue
            
        # 最新価格を取得 (1m足の終値が最も新しい)
        latest_price = ohlcv_data.get('1m', pd.DataFrame()).iloc[-1]['close'] if '1m' in ohlcv_data and not ohlcv_data['1m'].empty else None
        
        if latest_price is None or latest_price <= 0:
             # 最新価格が取得できない場合はスキップ
             logging.warning(f"⚠️ {symbol}: 最新価格が取得できないため、分析をスキップします。")
             continue
             
        # CCXTクライアントにOHLCVデータをキャッシュ (ポジション監視用など)
        if not hasattr(EXCHANGE_CLIENT, 'ohlcv_data'):
            EXCHANGE_CLIENT.ohlcv_data = {}
        EXCHANGE_CLIENT.ohlcv_data[symbol] = ohlcv_data

        # スコア計算
        signal = calculate_signal_score(ohlcv_data, symbol, macro_context, latest_price)
        
        if signal:
            potential_signals.append(signal)
    
    # スコアの高い順にソート
    potential_signals.sort(key=lambda x: x['score'], reverse=True)
    
    # グローバル変数に最新の分析結果を格納 (定時通知用)
    LAST_ANALYSIS_SIGNALS = potential_signals
    
    # 6. エントリーシグナルの処理
    executed_signals = []
    
    for signal in potential_signals[:TOP_SIGNAL_COUNT]:
        symbol = signal['symbol']
        side = signal['side']
        score = signal['score']
        current_time = time.time()
        
        # 冷却期間チェック
        last_trade_time = LAST_SIGNAL_TIME.get(symbol, 0.0)
        if current_time - last_trade_time < TRADE_SIGNAL_COOLDOWN:
            logging.info(f"ℹ️ {symbol} ({side.capitalize()}): 冷却期間 ({TRADE_SIGNAL_COOLDOWN/3600:.0f}時間) 中のためスキップします。")
            continue
            
        # 既存ポジションチェック (同じ銘柄のポジションがないか確認)
        has_existing_position = any(p['symbol'] == symbol for p in OPEN_POSITIONS)
        if has_existing_position:
            logging.info(f"ℹ️ {symbol}: 既存のポジションがあるため、新規エントリーをスキップします。")
            continue
            
        logging.critical(f"🚀 {symbol} ({side.capitalize()}): 総合スコア {score*100:.2f}。エントリー処理を開始します。")

        # リスク管理と注文実行
        final_signal, trade_result = await process_entry_signal(signal)
        
        if final_signal and trade_result and trade_result.get('status') == 'ok':
            executed_signals.append(final_signal)
            
            # 注文成功を通知
            current_threshold = get_current_threshold(macro_context) # 通知用に取得
            message = format_telegram_message(final_signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(message)
            
            # 冷却期間を更新
            LAST_SIGNAL_TIME[symbol] = current_time
            
            # トレードログを記録
            log_signal(final_signal, "Trade Entry", trade_result)
            
            # TOP_SIGNAL_COUNT が1なので、ここでループを終了
            break 
            
        elif trade_result and trade_result.get('status') == 'error':
             # 注文失敗を通知 (冷却期間は更新しない)
            current_threshold = get_current_threshold(macro_context) # 通知用に取得
            message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(message)
            
            # トレードログを記録 (失敗)
            log_signal(signal, "Trade Entry Failed", trade_result)
            
        
    # 7. 初回起動完了通知 (一度だけ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        current_threshold = get_current_threshold(macro_context)
        startup_message = format_startup_message(account_status, macro_context, len(monitoring_symbols), current_threshold)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    # 8. 定時分析結果の通知
    await notify_highest_analysis_score()
    
    # 9. WebShareへのデータ送信 (定期的に)
    global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
    current_time = time.time()
    
    if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        webshare_data = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'account_status': account_status,
            'macro_context': macro_context,
            'open_positions': OPEN_POSITIONS,
            'top_signals': potential_signals[:5],
            'bot_version': BOT_VERSION
        }
        await send_webshare_update(webshare_data)
        LAST_WEBSHARE_UPLOAD_TIME = current_time
        
    # 最終成功時間を更新
    LAST_SUCCESS_TIME = current_time
    logging.info("✅ メイン分析ループが完了しました。")


async def main_bot_scheduler():
    """
    メインの取引/分析ループを定期的に実行するスケジューラ。
    """
    global LOOP_INTERVAL
    
    try:
        # 0. クライアントの初期化 (成功するまでループ)
        while not IS_CLIENT_READY:
            logging.info("⏳ CCXTクライアントを初期化しています...")
            if await initialize_exchange_client():
                break
            logging.critical("❌ CCXTクライアントの初期化に失敗しました。10秒後に再試行します。")
            await asyncio.sleep(10)
            
        # 1. 無限ループでメインロジックを実行
        while True:
            try:
                # ログのタイムスタンプが更新されるように、メインループを実行
                await run_main_loop()
                
            except Exception as e:
                logging.critical(f"❌ 致命的なスケジューラエラーが発生しました: {e}", exc_info=True)
                
                # Telegram通知を送信
                error_time = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
                error_message = (
                    f"🚨 **致命的なスケジューラエラー通知**\n"
                    f"  - **日時**: {error_time} (JST)\n"
                    f"  - **エラー内容**: <code>{type(e).__name__}: {str(e)}</code>\n"
                    f"  - **再試行**: {LOOP_INTERVAL}秒後にメインループを再開します。\n"
                    f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
                    f"<i>Bot Ver: {BOT_VERSION}</i>"
                )
                await send_telegram_notification(error_message)
                
                # CCXTクライアントを強制的に再初期化 (接続不良対策)
                await initialize_exchange_client()

            # 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)

    except asyncio.CancelledError:
        logging.info("✅ メインボットスケジューラがキャンセルされました。")
    except Exception as e:
        logging.critical(f"❌ スケジューラ起動時に予期せぬエラーが発生しました: {e}", exc_info=True)


async def position_monitor_scheduler():
    """
    ポジション監視 (SL/TPトリガーチェック) を定期的に実行するスケジューラ。
    メインループとは独立して実行される。
    """
    try:
        # メインループが一度成功するまで待機
        while not IS_FIRST_MAIN_LOOP_COMPLETED:
            logging.info("⏳ メインループの初回完了を待機しています...")
            await asyncio.sleep(MONITOR_INTERVAL) 
        
        while True:
            # ポジションのSL/TPチェックと決済
            # 外部APIへのアクセス (fetch_tickers) が含まれるため、レートリミットに注意しつつ実行
            await position_monitor_and_update_sltp()
            
            await asyncio.sleep(MONITOR_INTERVAL) # 10秒ごとに監視

    except asyncio.CancelledError:
        logging.info("✅ ポジション監視スケジューラがキャンセルされました。")
    except Exception as e:
        logging.critical(f"❌ ポジション監視スケジューラで致命的なエラーが発生しました: {e}", exc_info=True)
        # スケジューラ自体のクラッシュを防ぐため、エラーを捕捉し、待機後に再開を試みる
        await asyncio.sleep(MONITOR_INTERVAL)
        # 再度スケジューラを実行する (ただし、これはFastAPIの`startup`時に一度しか呼ばれないため、再実行はできない)
        # そのため、無限ループ内でのエラー捕捉と再試行が重要となる
        
        
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
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        # Telegram通知はループ内で実行されるため、ここでは省略
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )

# サーバー起動コマンド (uvicorn main_render:app --host 0.0.0.0 --port 8000)
if __name__ == "__main__":
    # 環境変数からポートを取得 (Renderや他のPaaS対応)
    port = int(os.environ.get("PORT", 8000))
    # 💡 log_level='info' を明示的に設定
    uvicorn.run("main_render:app", host="0.0.0.0", port=port, log_level='info')
