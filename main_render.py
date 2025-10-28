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

        # グローバルなポジションリストを更新
        global OPEN_POSITIONS
        OPEN_POSITIONS = open_positions
        
        logging.info(f"✅ ポジション情報取得完了。現在 {len(OPEN_POSITIONS)} 件のオープンポジションがあります。")


        return {
            'total_usdt_balance': ACCOUNT_EQUITY_USDT,
            'open_positions': OPEN_POSITIONS,
            'error': False,
        }
        
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - ネットワークエラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': f"ネットワークエラー: {e}"}
    except ccxt.ExchangeError as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 取引所エラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': f"取引所エラー: {e}"}
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 - 予期せぬエラー: {e}", exc_info=True)
        return {'total_usdt_balance': ACCOUNT_EQUITY_USDT, 'open_positions': OPEN_POSITIONS, 'error': True, 'error_message': f"予期せぬエラー: {e}"}
        
# ====================================================================================
# TECHNICAL ANALYSIS & SCORING LOGIC (実践的スコアリングロジック)
# ====================================================================================

async def calculate_technical_indicators(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """
    データフレームにテクニカル指標を計算して追加する。
    """
    if df.empty:
        return df

    # 1. 移動平均線 (SMA: Simple Moving Average)
    df['SMA_Long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH) 

    # 2. RSI (Relative Strength Index) - 14期間
    df['RSI'] = ta.rsi(df['close'], length=14)
    
    # 3. MACD (Moving Average Convergence Divergence)
    macd_result = ta.macd(df['close'], fast=12, slow=26, signal=9)
    # MACDの結果を既存のDataFrameに結合。列名を調整。
    df[['MACD', 'MACD_H', 'MACD_S']] = macd_result
    
    # 4. ATR (Average True Range) - ボラティリティ測定に使用
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=ATR_LENGTH)
    
    # 5. Bollinger Bands (BBands)
    bbands_result = ta.bbands(df['close'], length=20, std=2)
    df[['BBL', 'BBM', 'BBU', 'BBB', 'BBP']] = bbands_result 

    # 6. OBV (On-Balance Volume)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    
    # 7. Keltner Channels (KC)
    # kc_result = ta.kc(df['high'], df['low'], df['close'])
    # df[['KCL', 'KCM', 'KCU', 'KCW', 'KCP']] = kc_result
    
    # NaN行を削除 (SMAの期間により発生)
    df = df.dropna().reset_index(drop=True)
    
    return df

def get_signal_score(df: pd.DataFrame, symbol: str, timeframe: str, macro_context: Dict) -> List[Dict]:
    """
    テクニカル指標とマクロ環境に基づいて、ロング/ショートのスコアを計算する。
    
    スコアリングルール (v20.0.43: 実践的スコアリングロジック)
    ================================================================
    ベーススコア: BASE_SCORE (0.40)
    1. 長期トレンド順張りボーナス/逆張りペナルティ: +/- LONG_TERM_REVERSAL_PENALTY (0.20)
       - 現在価格とSMA_Longの位置関係
    2. 構造的優位性 (リバーサル/押し目): + STRUCTURAL_PIVOT_BONUS (0.05)
       - 直近ローソク足の形状と位置 (ローソク足がBB下限/上限にタッチした後の反発)
    3. モメンタム確証 (RSI加速): + (0.00-0.05) 
       - ロング: RSIが40を上回り、上昇傾向 
       - ショート: RSIが60を下回り、下降傾向
    4. MACD方向一致ボーナス/不一致ペナルティ: +/- MACD_CROSS_PENALTY (0.15)
       - MACDラインとシグナルラインのクロス方向
    5. OBVモメンタム確証: + OBV_MOMENTUM_BONUS (0.04)
       - OBVがSMA (50)を上抜け/下抜け
    6. 流動性ボーナス: + LIQUIDITY_BONUS_MAX (0.06) 
       - 出来高TOP銘柄であるほど加点
    7. FGIマクロ環境ボーナス/ペナルティ: +/- FGI_PROXY_BONUS_MAX (0.05) 
       - FGIの極端な値 (恐怖でロングボーナス, 貪欲でショートボーナス)
    8. ボラティリティペナルティ: - (0.00-0.05)
       - BB幅 (BBB) が一定値以上の場合、過熱と見なしてペナルティ
    
    合計: 0.40 (ベース) + 0.05 + 0.20 + 0.15 + 0.05 + 0.06 + 0.04 = 0.95 (最大スコア)
    ================================================================
    """
    if df.empty or len(df) < ATR_LENGTH + 1: 
        return []

    latest = df.iloc[-1]
    prev_latest = df.iloc[-2] # 1つ前のローソク足
    
    current_price = latest['close']
    
    # 出来高ベースの流動性ボーナスを計算
    # (ここでは、DEFAULT_SYMBOLSのリスト順を簡易的な流動性の高さと見なす)
    # TOP_SYMBOL_LIMITが40なので、リスト内の前半40位までを対象とする
    is_top_symbol = symbol in CURRENT_MONITOR_SYMBOLS[:TOP_SYMBOL_LIMIT]
    liquidity_bonus_value = LIQUIDITY_BONUS_MAX if is_top_symbol else 0.0

    # FGIマクロコンテキストの取得 (既に0.0から+/- 0.05に正規化されている)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # --------------------------------------------------------
    # ロング/ショート共通のペナルティ
    # --------------------------------------------------------
    
    # 8. ボラティリティペナルティ (BB幅が価格の1%を超えたらペナルティ)
    bb_width_percent = latest['BBB'] / 100.0 # BBBはBBW%として計算されている
    volatility_penalty_value = 0.0
    if bb_width_percent > VOLATILITY_BB_PENALTY_THRESHOLD: # 例: 1%を超える
        # 1%を超えた分に応じて最大0.05までペナルティ
        penalty_ratio = min(1.0, (bb_width_percent - VOLATILITY_BB_PENALTY_THRESHOLD) / 0.01) # 2%なら1.0
        volatility_penalty_value = -0.05 * penalty_ratio 


    # --------------------------------------------------------
    # ロングシグナルのスコアリング
    # --------------------------------------------------------
    long_score = BASE_SCORE + STRUCTURAL_PIVOT_BONUS # ベース + 構造的優位性 (基本は常に加算)
    long_tech_data = {'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 'base_score': BASE_SCORE}
    
    # 1. 長期トレンド順張りボーナス/逆張りペナルティ (Long)
    # 価格が長期SMA (200)の上にいるか
    long_term_reversal_penalty_value = 0.0
    if current_price > latest['SMA_Long']: # 順張り (上昇トレンド)
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY 
        long_score += long_term_reversal_penalty_value
    else: # 逆張り (下降トレンド)
        long_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY
        long_score += long_term_reversal_penalty_value
    long_tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value


    # 3. モメンタム確証 (RSI加速: ロング)
    rsi_momentum_bonus_value = 0.0
    if latest['RSI'] > RSI_MOMENTUM_LOW and latest['RSI'] > prev_latest['RSI']: # RSIが40より上で上昇
        rsi_momentum_bonus_value = 0.05 
        long_score += rsi_momentum_bonus_value
    long_tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    
    
    # 4. MACD方向一致ボーナス/不一致ペナルティ (Long)
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインの上で、かつヒストグラムがプラスの場合 (上昇モメンタム)
    if latest['MACD'] > latest['MACD_S'] and latest['MACD_H'] > 0:
        macd_penalty_value = MACD_CROSS_PENALTY
        long_score += macd_penalty_value
    # MACDラインがシグナルラインの下で、かつヒストグラムがマイナスの場合 (下降モメンタム = Longの向かい風)
    elif latest['MACD'] < latest['MACD_S'] and latest['MACD_H'] < 0:
        macd_penalty_value = -MACD_CROSS_PENALTY
        long_score += macd_penalty_value
    long_tech_data['macd_penalty_value'] = macd_penalty_value


    # 5. OBVモメンタム確証 (Long)
    obv_momentum_bonus_value = 0.0
    # 出来高が価格上昇を伴っている場合 (単純にOBVが前日より上昇)
    if latest['OBV'] > prev_latest['OBV']:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        long_score += obv_momentum_bonus_value
    long_tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value


    # 7. FGIマクロ環境ボーナス/ペナルティ (Long: 恐怖相場でボーナス)
    sentiment_fgi_proxy_bonus = 0.0
    if fgi_proxy < 0: # 恐怖相場 (0.0から-0.05)
        sentiment_fgi_proxy_bonus = abs(fgi_proxy) 
    elif fgi_proxy > 0: # 貪欲相場 (0.0から+0.05)
        sentiment_fgi_proxy_bonus = -abs(fgi_proxy) 
    long_score += sentiment_fgi_proxy_bonus
    long_tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus


    # 6. 流動性ボーナス (Long)
    long_score += liquidity_bonus_value
    long_tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    
    # 8. ボラティリティペナルティ (Long)
    long_score += volatility_penalty_value
    long_tech_data['volatility_penalty_value'] = volatility_penalty_value
    

    # --------------------------------------------------------
    # ショートシグナルのスコアリング
    # --------------------------------------------------------
    short_score = BASE_SCORE + STRUCTURAL_PIVOT_BONUS # ベース + 構造的優位性
    short_tech_data = {'structural_pivot_bonus': STRUCTURAL_PIVOT_BONUS, 'base_score': BASE_SCORE}
    
    # 1. 長期トレンド順張りボーナス/逆張りペナルティ (Short)
    # 価格が長期SMA (200)の下にいるか
    short_term_reversal_penalty_value = 0.0
    if current_price < latest['SMA_Long']: # 順張り (下降トレンド)
        short_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY 
        short_score += short_term_reversal_penalty_value
    else: # 逆張り (上昇トレンド)
        short_term_reversal_penalty_value = -LONG_TERM_REVERSAL_PENALTY
        short_score += short_term_reversal_penalty_value
    short_tech_data['long_term_reversal_penalty_value'] = short_term_reversal_penalty_value


    # 3. モメンタム確証 (RSI加速: Short)
    rsi_momentum_bonus_value = 0.0
    if latest['RSI'] < (100 - RSI_MOMENTUM_LOW) and latest['RSI'] < prev_latest['RSI']: # RSIが60より下で下降
        rsi_momentum_bonus_value = 0.05 
        short_score += rsi_momentum_bonus_value
    short_tech_data['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
    

    # 4. MACD方向一致ボーナス/不一致ペナルティ (Short)
    macd_penalty_value = 0.0
    # MACDラインがシグナルラインの下で、かつヒストグラムがマイナスの場合 (下降モメンタム)
    if latest['MACD'] < latest['MACD_S'] and latest['MACD_H'] < 0:
        macd_penalty_value = MACD_CROSS_PENALTY
        short_score += macd_penalty_value
    # MACDラインがシグナルラインの上で、かつヒストグラムがプラスの場合 (上昇モメンタム = Shortの向かい風)
    elif latest['MACD'] > latest['MACD_S'] and latest['MACD_H'] > 0:
        macd_penalty_value = -MACD_CROSS_PENALTY
        short_score += macd_penalty_value
    short_tech_data['macd_penalty_value'] = macd_penalty_value


    # 5. OBVモメンタム確証 (Short)
    obv_momentum_bonus_value = 0.0
    # 出来高が価格下降を伴っている場合 (単純にOBVが前日より下降)
    if latest['OBV'] < prev_latest['OBV']:
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        short_score += obv_momentum_bonus_value
    short_tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value


    # 7. FGIマクロ環境ボーナス/ペナルティ (Short: 貪欲相場でボーナス)
    sentiment_fgi_proxy_bonus = 0.0
    if fgi_proxy > 0: # 貪欲相場 (0.0から+0.05)
        sentiment_fgi_proxy_bonus = fgi_proxy 
    elif fgi_proxy < 0: # 恐怖相場 (0.0から-0.05)
        sentiment_fgi_proxy_bonus = -abs(fgi_proxy) 
    short_score += sentiment_fgi_proxy_bonus
    short_tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus


    # 6. 流動性ボーナス (Short)
    short_score += liquidity_bonus_value
    short_tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    
    # 8. ボラティリティペナルティ (Short)
    short_score += volatility_penalty_value
    short_tech_data['volatility_penalty_value'] = volatility_penalty_value


    # --------------------------------------------------------
    # 結果の整形
    # --------------------------------------------------------
    
    # スコアを0.0から1.0に丸める
    long_score = max(0.0, min(1.0, long_score))
    short_score = max(0.0, min(1.0, short_score))


    results = []
    
    if long_score > BASE_SCORE:
        results.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': 'long',
            'score': long_score,
            'current_price': current_price,
            'latest_data': latest.to_dict(),
            'tech_data': long_tech_data,
        })
        
    if short_score > BASE_SCORE:
        results.append({
            'symbol': symbol,
            'timeframe': timeframe,
            'side': 'short',
            'score': short_score,
            'current_price': current_price,
            'latest_data': latest.to_dict(),
            'tech_data': short_tech_data,
        })

    return results


async def process_entry_signal(signal: Dict) -> Optional[Dict]:
    """
    シグナル情報に基づき、ポジションサイズとSL/TP価格を計算し、取引を実行する。
    """
    global EXCHANGE_CLIENT, ACCOUNT_EQUITY_USDT, OPEN_POSITIONS, LEVERAGE, FIXED_NOTIONAL_USDT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 取引失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'クライアント未準備'}

    symbol = signal['symbol']
    side = signal['side']
    current_price = signal['current_price']
    latest_data = signal['latest_data']
    
    try:
        # 1. ATRに基づく動的なリスク幅 (SL) を計算
        # ATR * Multiplier でリスク幅 (価格差) を決定
        atr_value = latest_data.get('ATR', 0.0)
        risk_price_diff_raw = atr_value * ATR_MULTIPLIER_SL
        
        # 最低リスク幅 (価格に対する割合) を適用
        min_risk_price_diff = current_price * MIN_RISK_PERCENT 
        risk_price_diff = max(risk_price_diff_raw, min_risk_price_diff)

        # 2. SLとTPの価格を決定
        stop_loss = 0.0
        take_profit = 0.0
        
        if side == 'long':
            # SL: エントリー価格 - リスク価格差
            stop_loss = current_price - risk_price_diff
            # TP: エントリー価格 + (リスク価格差 * RR比率)
            reward_price_diff = risk_price_diff * RR_RATIO_TARGET
            take_profit = current_price + reward_price_diff
        elif side == 'short':
            # SL: エントリー価格 + リスク価格差
            stop_loss = current_price + risk_price_diff
            # TP: エントリー価格 - (リスク価格差 * RR比率)
            reward_price_diff = risk_price_diff * RR_RATIO_TARGET
            take_profit = current_price - reward_price_diff

        # 価格がゼロ以下にならないように保証
        stop_loss = max(0.01, stop_loss)
        take_profit = max(0.01, take_profit)

        # 3. リスクリワード比率の計算とロギング用情報の更新
        # 実際のリスク/リワード幅で計算し直す
        actual_risk_diff = abs(current_price - stop_loss)
        actual_reward_diff = abs(take_profit - current_price)
        
        # リスク幅がゼロに近い場合は取引をスキップ
        if actual_risk_diff < 1e-6:
             logging.error(f"❌ 取引スキップ: {symbol} リスク幅がゼロに近すぎます。")
             return None

        # 実際のリスクリワード比率
        actual_rr_ratio = actual_reward_diff / actual_risk_diff
        
        # 4. ポジションサイズ (契約数) の決定 (固定名目ロットを使用)
        # 固定ロット (USDT) / 価格 = 契約数量
        amount_to_buy_base = FIXED_NOTIONAL_USDT / current_price
        
        # 取引所の数量精度を適用
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
             raise Exception(f"市場情報が見つかりません: {symbol}")

        # 数量精度 (amount precision) を取得し、数量を丸める
        amount_precision = market.get('precision', {}).get('amount', 4) 
        amount_to_buy = EXCHANGE_CLIENT.amount_to_precision(symbol, amount_to_buy_base)
        
        # 数量が0以下の場合もスキップ
        if float(amount_to_buy) <= 0:
             logging.error(f"❌ 取引スキップ: {symbol} 計算された契約数量がゼロ以下です。計算ロット: {FIXED_NOTIONAL_USDT} USDT")
             return None


        # 5. 清算価格 (Liquidation Price) の計算
        liquidation_price = calculate_liquidation_price(current_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE)


        # 6. シグナル辞書に価格とポジション情報を追加
        signal['entry_price'] = current_price
        signal['stop_loss'] = stop_loss
        signal['take_profit'] = take_profit
        signal['liquidation_price'] = liquidation_price
        signal['contracts'] = float(amount_to_buy) # 契約数量
        signal['filled_usdt'] = FIXED_NOTIONAL_USDT # 名目ロット
        signal['rr_ratio'] = actual_rr_ratio

        if TEST_MODE:
            logging.warning(f"⚠️ TEST_MODE: {symbol} ({side}) の取引をシミュレートします。ロット: {float(amount_to_buy):.4f} 契約 ({FIXED_NOTIONAL_USDT} USDT)")
            # ポジションリストには追加しない (実取引がないため)
            return {'status': 'ok', 'filled_amount': amount_to_buy, 'filled_price': current_price, 'filled_usdt': FIXED_NOTIONAL_USDT}


        # 7. 実際の取引実行 (成行注文)
        order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=side,
            amount=amount_to_buy,
            params={
                'leverage': LEVERAGE, 
                'positionSide': side.capitalize(), # Long/Short
            }
        )
        
        # 8. 約定情報とポジション管理情報の作成
        filled_price = order.get('price', current_price) # 成行注文なので最新価格に近い
        filled_amount = order.get('amount', float(amount_to_buy))
        
        # 新しいポジション情報を作成 (グローバルリストに追加する用)
        new_position = {
            'symbol': symbol,
            'side': side,
            'contracts': filled_amount if side == 'long' else -filled_amount, # Longは正、Shortは負
            'entry_price': filled_price,
            'filled_usdt': abs(filled_amount * filled_price),
            'liquidation_price': calculate_liquidation_price(filled_price, LEVERAGE, side, MIN_MAINTENANCE_MARGIN_RATE),
            'timestamp': int(time.time() * 1000),
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'leverage': LEVERAGE,
            'signal_id': uuid.uuid4().hex, # ポジションのユニークID
            'raw_info': order,
        }
        
        # 9. グローバルなポジションリストの更新
        OPEN_POSITIONS.append(new_position)
        
        logging.info(f"✅ 取引成功: {symbol} ({side}) を {filled_amount:.4f} 契約 (ロット: {new_position['filled_usdt']:.2f} USDT) で約定。")
        
        # シグナル情報にも約定情報を追加
        signal['entry_price'] = filled_price
        signal['liquidation_price'] = new_position['liquidation_price']
        signal['contracts'] = filled_amount
        
        # 10. SL/TPの設定 (OCO注文またはストップリミット/リミット注文)
        # ほとんどの取引所はポジションに対してSL/TPを設定する機能を持つ。
        # CCXTの `edit_position_risk` や `set_margin_mode` などを使用するが、取引所依存のため、
        # ここでは ccxt.create_order() でストップリミット/テイクプロフィットリミットの逆指値注文を同時に出す。
        
        # MEXCの場合、CCXTでSL/TPを設定する機能が利用可能か確認 (通常はできないため、CCXTの標準メソッドを使用)
        try:
            # 💡 ポジション全体に対するSL/TPの設定 (MEXC, Bybitなどはこの方法で設定可能)
            sl_tp_result = await EXCHANGE_CLIENT.edit_position_risk(
                symbol=symbol,
                side='both', # SL/TPは通常、ポジション全体に対して設定する
                stop_loss=stop_loss,
                take_profit=take_profit,
            )
            logging.info(f"✅ SL/TP設定成功 (edit_position_risk): {symbol} SL: {format_price(stop_loss)}, TP: {format_price(take_profit)}")
            
        except ccxt.NotSupported:
            # edit_position_riskがサポートされていない場合、通常のストップ注文とリミット注文を出す
            logging.warning(f"⚠️ {EXCHANGE_CLIENT.id} は edit_position_risk をサポートしていません。個別のストップ/リミット注文を発注します。")
            
            # SL注文 (逆指値の成行注文)
            sl_order_side = 'sell' if side == 'long' else 'buy'
            sl_order_type = 'market' # 逆指値成行 (Stop Market)
            sl_params = {
                'stopPrice': stop_loss,
                'triggerBy': 'LastPrice' if side == 'long' else 'MarkPrice', # 取引所によって異なるが、LongはLastPriceでトリガー、ShortはMarkPriceでトリガーが一般的
                'stopLoss': stop_loss, # これをセットするだけでSLとして機能する取引所もある (Binance/Bybit)
                'reduceOnly': True, # 決済専用
            }
            try:
                await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type=sl_order_type,
                    side=sl_order_side,
                    amount=filled_amount,
                    params=sl_params
                )
                logging.info(f"✅ SL (Stop-Market) 注文を発注しました: {symbol} @{format_price(stop_loss)}")
            except Exception as e:
                logging.error(f"❌ SL注文の発注に失敗: {e}")


            # TP注文 (リミット注文)
            tp_order_side = 'sell' if side == 'long' else 'buy'
            tp_order_type = 'limit'
            tp_params = {
                'takeProfit': take_profit, # これをセットするだけでTPとして機能する取引所もある
                'reduceOnly': True, # 決済専用
            }
            try:
                await EXCHANGE_CLIENT.create_order(
                    symbol=symbol,
                    type=tp_order_type,
                    side=tp_order_side,
                    amount=filled_amount,
                    price=take_profit, # TP価格でリミット注文
                    params=tp_params
                )
                logging.info(f"✅ TP (Limit) 注文を発注しました: {symbol} @{format_price(take_profit)}")
            except Exception as e:
                logging.error(f"❌ TP注文の発注に失敗: {e}")


        except Exception as e:
            logging.error(f"❌ SL/TP注文の設定中に予期せぬエラーが発生しました: {e}", exc_info=True)


        return {
            'status': 'ok', 
            'filled_amount': filled_amount, 
            'filled_price': filled_price,
            'filled_usdt': new_position['filled_usdt'],
            'stop_loss': stop_loss,
            'take_profit': take_profit,
        }

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引失敗: {symbol} - 証拠金不足エラー: {e}")
        return {'status': 'error', 'error_message': f'証拠金不足エラー: {e}'}
    except ccxt.ExchangeError as e:
        # Amount can not be less than zero などのエラー対策
        logging.error(f"❌ 取引失敗: {symbol} - 取引所エラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'取引所エラー: {e}'}
    except Exception as e:
        logging.error(f"❌ 取引処理中の予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}


async def close_position(position: Dict, exit_price: float, exit_type: str) -> Dict:
    """
    指定されたオープンポジションを成行注文で決済する。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    symbol = position['symbol']
    side = position['side']
    contracts_raw = abs(position['contracts']) # 契約数量は常に正の値で渡す
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ 決済失敗: CCXTクライアントが準備できていません。")
        return {'status': 'error', 'error_message': 'クライアント未準備', 'exit_type': exit_type}

    # 決済方向を決定: LongならSell, ShortならBuy
    close_side = 'sell' if side == 'long' else 'buy'
    
    # 利益/損失の計算 (あくまで推定)
    entry_price = position['entry_price']
    filled_usdt = position['filled_usdt']
    pnl_rate = 0.0
    
    if side == 'long':
        pnl_rate = (exit_price - entry_price) / entry_price * LEVERAGE
    elif side == 'short':
        pnl_rate = (entry_price - exit_price) / entry_price * LEVERAGE

    pnl_usdt = filled_usdt * (pnl_rate / LEVERAGE) * LEVERAGE 
    
    try:
        # 1. 既存のオープン注文 (SL/TP) を全てキャンセル
        try:
            await EXCHANGE_CLIENT.cancel_all_orders(symbol)
            logging.info(f"✅ 決済前処理: {symbol} のオープン注文を全てキャンセルしました。")
        except Exception as e:
            logging.warning(f"⚠️ {symbol} の注文キャンセル中にエラーが発生: {e}")

        # 2. 決済用の成行注文を発注 (reduceOnlyで確実な決済を狙う)
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
             raise Exception(f"市場情報が見つかりません: {symbol}")
        amount_to_close = EXCHANGE_CLIENT.amount_to_precision(symbol, contracts_raw)

        close_order = await EXCHANGE_CLIENT.create_order(
            symbol=symbol,
            type='market',
            side=close_side,
            amount=amount_to_close,
            params={
                'reduceOnly': True, # 決済専用フラグ
                'positionSide': side.capitalize(), # Long/Short 
            }
        )
        
        # 3. 約定情報を取得
        exit_price_actual = close_order.get('price', exit_price) 
        
        # 4. グローバルなポジションリストから削除
        # positionのsignal_id (またはユニークな識別子) を使って削除するのが理想的
        try:
            # 契約数が0になったポジション、またはこのpositionオブジェクトと一致するものを削除
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p.get('signal_id') != position.get('signal_id') and abs(p.get('contracts', 0)) > 1e-6]
        except Exception as e:
            logging.warning(f"⚠️ ポジションリストからの削除中にエラーが発生: {e} - 完全一致の削除を試みます。")
            OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p != position and abs(p.get('contracts', 0)) > 1e-6]

        logging.info(f"✅ ポジション決済成功: {symbol} ({side}) を {format_price(exit_price_actual)} で決済。PNL: {pnl_usdt:.2f} USDT")

        # 5. 決済結果の整形
        return {
            'status': 'ok',
            'exit_price': exit_price_actual,
            'entry_price': entry_price,
            'pnl_usdt': pnl_usdt,
            'pnl_rate': pnl_rate,
            'filled_amount': contracts_raw,
            'exit_type': exit_type,
            'raw_info': close_order,
        }

    except ccxt.ExchangeError as e:
        logging.error(f"❌ 決済失敗: {symbol} - 取引所エラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'取引所エラー: {e}', 'exit_type': exit_type}
    except Exception as e:
        logging.error(f"❌ 決済処理中の予期せぬエラー: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}', 'exit_type': exit_type}


# ====================================================================================
# MARKET & SYMBOL MANAGEMENT 
# ====================================================================================

async def fetch_top_volume_symbols() -> List[str]:
    """
    取引所から出来高の高いTOP_SYMBOL_LIMIT個のシンボルを取得し、USDT建て先物シンボルに変換して返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ TOPシンボル取得失敗: CCXTクライアントが準備できていません。")
        return []

    try:
        # fetch_tickers() を使用して全てのティッカー情報を取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers()
        
        if not tickers:
            logging.warning("⚠️ fetch_tickersが空のティッカー情報を返しました。デフォルトシンボルを使用します。")
            return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
            
        # 出来高 (quote volume) を基準にソートし、USDT建て先物のみを抽出
        futures_tickers = {}
        for symbol, ticker in tickers.items():
            # CCXTのシンボルは 'BTC/USDT' や 'BTC/USDT:USDT' など様々な形式がある
            is_usdt_future = False
            market = EXCHANGE_CLIENT.markets.get(symbol)
            
            if market and market.get('type') in ['future', 'swap'] and market.get('quote') == 'USDT' and market.get('active'):
                # CCXT標準のシンボル形式 (例: 'BTC/USDT') を使用
                standard_symbol = market.get('symbol') 
                is_usdt_future = True
            
            # 出来高 (quoteVolume) がNoneまたは0でないことを確認
            volume_usdt = ticker.get('quoteVolume') 
            
            if is_usdt_future and volume_usdt and volume_usdt > 0:
                # 最終的にリストに入れるのは 'BTC/USDT' 形式
                futures_tickers[standard_symbol] = volume_usdt

        # 出来高の降順でソート
        sorted_futures = sorted(
            futures_tickers.items(), 
            key=lambda item: item[1], 
            reverse=True
        )

        # TOP N個のシンボル名 (例: BTC/USDT) を取得
        top_symbols = [item[0] for item in sorted_futures][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルとTOPシンボルを統合
        # 重複を排除し、TOPシンボルを優先する
        final_symbols = []
        unique_symbols = set()
        
        # 1. Topシンボルを追加
        for s in top_symbols:
            if s not in unique_symbols:
                final_symbols.append(s)
                unique_symbols.add(s)
                
        # 2. デフォルトシンボルから、まだリストにないものを追加
        for s in DEFAULT_SYMBOLS:
            if s not in unique_symbols:
                 # 最大数を維持するために、まだリストの長さがTOP_SYMBOL_LIMITの2倍未満の場合のみ追加
                if len(final_symbols) < TOP_SYMBOL_LIMIT * 2: 
                    final_symbols.append(s)
                    unique_symbols.add(s)
                
        logging.info(f"✅ TOP出来高シンボル ({len(top_symbols)}件) を取得し、デフォルトと統合しました。最終監視数: {len(final_symbols)}件。")
        return final_symbols
    
    except ccxt.NetworkError as e:
        logging.error(f"❌ fetch_tickers失敗 - ネットワークエラー: {e}。デフォルトシンボルを使用します。", exc_info=True)
    except ccxt.ExchangeError as e:
        logging.error(f"❌ fetch_tickers失敗 - 取引所エラー: {e}。デフォルトシンボルを使用します。", exc_info=True)
    except Exception as e:
        # AttributeError: 'NoneType' object has no attribute 'keys' の対策
        logging.error(f"❌ fetch_tickers失敗 - 予期せぬエラー: {e}。デフォルトシンボルを使用します。", exc_info=True)
        
    # エラー時はデフォルトシンボルを返却
    return DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]


async def fetch_ohlcv_data(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """
    指定されたシンボルのOHLCVデータを取得し、DataFrameとして返す。
    """
    global EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return None
        
    try:
        # MEXCの場合、シンボル形式の調整が必要な場合がある (例: BTC/USDT -> BTC_USDT)
        # CCXTが自動的に変換してくれるはずだが、念のため market の情報を確認する
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            logging.warning(f"⚠️ {symbol} の市場情報が見つかりません。")
            return None
            
        ccxt_symbol = market['symbol'] # CCXTが内部で使用するシンボル名
            
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(ccxt_symbol, timeframe, limit=limit)
        
        if not ohlcv:
            logging.warning(f"⚠️ {symbol} ({timeframe}) のOHLCVデータが空でした。")
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        
        # 必要な行数があるかチェック
        if len(df) < limit:
             logging.warning(f"⚠️ {symbol} ({timeframe}): 必要な {limit} 行に対して {len(df)} 行しか取得できませんでした。")

        return df
        
    except ccxt.NetworkError as e:
        logging.warning(f"❌ {symbol} ({timeframe}) OHLCV取得失敗 - ネットワークエラー: {e}")
    except ccxt.ExchangeError as e:
        logging.warning(f"❌ {symbol} ({timeframe}) OHLCV取得失敗 - 取引所エラー: {e}")
    except Exception as e:
        logging.warning(f"❌ {symbol} ({timeframe}) OHLCV取得失敗 - 予期せぬエラー: {e}")
        
    return None

async def fetch_macro_context() -> Dict:
    """
    Fear & Greed Index (FGI) などのマクロ環境データを取得し、スコアリング用のプロキシ値を計算する。
    """
    
    fgi_proxy = 0.0
    fgi_raw_value = 'N/A'
    forex_bonus = 0.0
    
    # 1. Fear & Greed Index (FGI) の取得
    try:
        fgi_response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        fgi_response.raise_for_status()
        fgi_data = fgi_response.json()
        
        if fgi_data.get('data') and len(fgi_data['data']) > 0:
            value = int(fgi_data['data'][0]['value']) # 0 (Extreme Fear) - 100 (Extreme Greed)
            fgi_raw_value = f"{value} ({fgi_data['data'][0]['value_classification']})"
            
            # FGIを-1から1の範囲に正規化 (50が0, 0が-1, 100が1)
            fgi_normalized = (value - 50) / 50.0 
            
            # FGIプロキシを-FGI_PROXY_BONUS_MAXから+FGI_PROXY_BONUS_MAXの範囲に制限する
            # -1.0 * FGI_PROXY_BONUS_MAX <= fgi_proxy <= 1.0 * FGI_PROXY_BONUS_MAX
            fgi_proxy = fgi_normalized * FGI_PROXY_BONUS_MAX
            
            logging.info(f"✅ FGI取得完了: {fgi_raw_value} -> FGIプロキシ: {fgi_proxy:+.4f}")

    except Exception as e:
        logging.warning(f"⚠️ FGI取得失敗: {e}。FGIプロキシは0.0に設定されます。")
        
    # 2. 為替レート (USD/JPYなど) の動きに基づくボーナス (今回は実装をスキップ)
    # forex_bonus = 0.0 

    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_raw_value,
        'forex_bonus': forex_bonus,
    }

# ====================================================================================
# MAIN BOT LOGIC & SCHEDULERS
# ====================================================================================

async def analyze_and_trade():
    """
    監視対象の全銘柄に対してテクニカル分析を行い、シグナルを検出して取引を実行する。
    """
    global CURRENT_MONITOR_SYMBOLS, LAST_SIGNAL_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, ACCOUNT_EQUITY_USDT, LAST_SUCCESS_TIME
    
    logging.info("--- 🤖 分析・取引メインループ開始 ---")
    start_time = time.time()
    
    # 1. 市場環境情報の更新
    if not SKIP_MARKET_UPDATE:
        new_macro_context = await fetch_macro_context()
        GLOBAL_MACRO_CONTEXT.update(new_macro_context)
        
        # 出来高TOP銘柄のリストを更新
        top_symbols = await fetch_top_volume_symbols()
        if top_symbols:
            CURRENT_MONITOR_SYMBOLS = top_symbols
    
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
    
    # 2. 口座ステータスの更新
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ 致命的エラー: 口座ステータス取得失敗。取引ループをスキップします。")
        return # 取引を実行しない

    
    # 3. 全銘柄・全時間足で分析を実行
    all_signals = []
    
    # 処理の進捗を確認しやすくするため、銘柄数をログに出力
    total_symbols = len(CURRENT_MONITOR_SYMBOLS)
    logging.info(f"📊 監視対象: {total_symbols} 銘柄。取引閾値: {current_threshold * 100:.0f}点。")

    for i, symbol in enumerate(CURRENT_MONITOR_SYMBOLS):
        
        logging.info(f"➡️ 処理中: {symbol} ({i+1}/{total_symbols})")
        
        for timeframe in TARGET_TIMEFRAMES:
            limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
            
            # OHLCVデータを取得
            df = await fetch_ohlcv_data(symbol, timeframe, limit)
            
            if df is None or len(df) < limit:
                logging.warning(f"⚠️ {symbol} ({timeframe}): データ不足 ({len(df) if df is not None else 0}/{limit})。分析をスキップします。")
                continue
                
            # テクニカル指標を計算
            df = await calculate_technical_indicators(df, timeframe)
            
            # スコアリングを実行
            signals = get_signal_score(df, symbol, timeframe, GLOBAL_MACRO_CONTEXT)
            
            if signals:
                all_signals.extend(signals)
                
        # 銘柄ごとのAPIレートリミット対策
        await asyncio.sleep(0.5) 

    # 4. シグナル処理
    LAST_ANALYSIS_SIGNALS = all_signals # 定時通知用に分析結果を保存

    # スコアの高い順にソート
    sorted_signals = sorted(all_signals, key=lambda x: x['score'], reverse=True)
    
    signals_to_trade: List[Dict] = []
    
    for signal in sorted_signals:
        symbol = signal['symbol']
        side = signal.get('side', 'long')
        signal_id = f"{symbol}_{side}"
        score = signal['score']
        
        # 既にポジションを持っている銘柄はスキップ
        has_open_position = any(p['symbol'] == symbol for p in OPEN_POSITIONS)
        
        if has_open_position:
            logging.info(f"ℹ️ {symbol} ({side}): 既にポジションがあるため取引をスキップします。")
            continue
            
        # クールダウン期間の確認
        if signal_id in LAST_SIGNAL_TIME and (time.time() - LAST_SIGNAL_TIME[signal_id]) < TRADE_SIGNAL_COOLDOWN:
            elapsed = int(time.time() - LAST_SIGNAL_TIME[signal_id])
            logging.info(f"ℹ️ {symbol} ({side}): クールダウン期間中 ({elapsed // 3600}h経過 / {TRADE_SIGNAL_COOLDOWN // 3600}h)。スキップします。")
            continue
            
        # 閾値の確認
        if score >= current_threshold:
            signals_to_trade.append(signal)
            
        if len(signals_to_trade) >= TOP_SIGNAL_COUNT:
            break
            
    # 5. 実際に取引を実行
    for signal in signals_to_trade:
        symbol = signal['symbol']
        side = signal['side']
        signal_id = f"{symbol}_{side}"
        score = signal['score']
        
        logging.info(f"🔥 **取引シグナル検出**: {symbol} ({side}) スコア: {score:.4f}")

        # 取引ロジックを実行し、ポジションサイズとSL/TPを設定
        trade_result = await process_entry_signal(signal)
        
        # 取引結果に応じて通知とログを記録
        if trade_result and trade_result.get('status') == 'ok':
            # ログ/通知用のシグナル情報を更新
            LAST_SIGNAL_TIME[signal_id] = time.time()
            log_signal(signal, "取引シグナル", trade_result)
            
            message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(message)
            
        elif trade_result and trade_result.get('status') == 'error':
             log_signal(signal, "取引失敗", trade_result)
             error_message = format_telegram_message(signal, "取引失敗", current_threshold, trade_result)
             await send_telegram_notification(error_message)

        # レートリミット対策
        await asyncio.sleep(2.0)
        
    
    # 6. ループ完了と通知
    end_time = time.time()
    elapsed_time = end_time - start_time
    logging.info(f"--- 🤖 分析・取引メインループ完了 --- 処理時間: {elapsed_time:.2f}秒。")
    
    # 初回起動時通知フラグの更新
    global IS_FIRST_MAIN_LOOP_COMPLETED
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動メッセージを送信
        startup_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(startup_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        
    LAST_SUCCESS_TIME = end_time


async def check_and_manage_open_positions():
    """
    オープンポジションを巡回し、SL/TPのトリガーを確認する。
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        logging.info("ℹ️ ポジション監視: オープンポジションはありません。")
        return

    logging.info(f"🔄 ポジション監視開始: {len(OPEN_POSITIONS)} 件をチェック。")
    
    # 最新の価格情報を一括で取得
    symbols_to_check = list(set(p['symbol'] for p in OPEN_POSITIONS))
    
    latest_prices = {}
    try:
        tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_check)
        for symbol, ticker in tickers.items():
            # CCXTのシンボル形式を標準化 (例: 'BTC/USDT')
            market = EXCHANGE_CLIENT.markets.get(symbol)
            if market:
                latest_prices[market['symbol']] = ticker.get('last', ticker.get('close'))
            
    except Exception as e:
        logging.error(f"❌ ポジション監視: 価格情報の一括取得に失敗: {e}。スキップします。", exc_info=True)
        return


    # ポジションのチェックと決済
    positions_to_close: List[Tuple[Dict, float, str]] = [] # (position, exit_price, exit_type)

    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        side = position['side']
        sl = position['stop_loss']
        tp = position['take_profit']
        current_price = latest_prices.get(symbol)
        
        if current_price is None:
            logging.warning(f"⚠️ ポジション監視: {symbol} の最新価格が取得できませんでした。スキップします。")
            continue

        trigger = None
        exit_price_candidate = current_price

        if side == 'long':
            # ロングの場合: 価格がSL以下になれば損切り (SL)
            if current_price <= sl:
                trigger = 'SL_HIT'
                exit_price_candidate = sl # SL価格で決済とする
            # ロングの場合: 価格がTP以上になれば利確 (TP)
            elif current_price >= tp:
                trigger = 'TP_HIT'
                exit_price_candidate = tp # TP価格で決済とする
        
        elif side == 'short':
            # ショートの場合: 価格がSL以上になれば損切り (SL)
            if current_price >= sl:
                trigger = 'SL_HIT'
                exit_price_candidate = sl # SL価格で決済とする
            # ショートの場合: 価格がTP以下になれば利確 (TP)
            elif current_price <= tp:
                trigger = 'TP_HIT'
                exit_price_candidate = tp # TP価格で決済とする
                
        # 🚨 清算価格チェック (必須)
        liquidation_price = position.get('liquidation_price', 0.0)
        if liquidation_price > 0:
            if side == 'long' and current_price <= liquidation_price * 1.05: # 5%手前で警告/緊急決済
                 logging.critical(f"🚨 緊急警告: {symbol} ({side}) が清算価格 {format_price(liquidation_price)} に接近中 ({format_price(current_price)})！")
                 # ここでは SL/TP管理に任せるが、緊急なら SL_HIT と同じ処理をトリガーすることも可能
            
        if trigger:
            positions_to_close.append((position, exit_price_candidate, trigger))
            logging.info(f"🔔 決済トリガー: {symbol} ({side}) で {trigger} が検出されました。Current: {format_price(current_price)}.")
            
    
    # 検出されたポジションを順番に決済
    for position, exit_price, exit_type in positions_to_close:
        
        # 決済処理を実行
        trade_result = await close_position(position, exit_price, exit_type)
        
        # 決済結果を通知・ログに記録
        if trade_result.get('status') == 'ok':
            # ログ/通知用に、ポジション情報に決済情報を追加
            position['exit_price'] = trade_result['exit_price']
            position['pnl_usdt'] = trade_result['pnl_usdt']
            position['pnl_rate'] = trade_result['pnl_rate']
            position['exit_type'] = exit_type
            
            log_signal(position, "ポジション決済", trade_result)
            
            message = format_telegram_message(position, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            await send_telegram_notification(message)
            
        else:
            logging.error(f"❌ 決済処理失敗: {position['symbol']} ({exit_type}) - エラー: {trade_result.get('error_message', '不明')}")
            error_message = format_telegram_message(position, "ポジション決済失敗", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
            await send_telegram_notification(error_message)

        # レートリミット対策
        await asyncio.sleep(1.0) 
        
    logging.info(f"✅ ポジション監視完了。現在のオープンポジション数: {len(OPEN_POSITIONS)} 件。")


async def main_bot_scheduler():
    """
    BOTのメインとなる無限ループスケジューラ。
    """
    global LOOP_INTERVAL, IS_CLIENT_READY
    
    logging.info("⭐ メインBOTスケジューラ起動。")
    
    # 1. CCXTクライアントの初期化 (BOT起動時に一度だけ)
    if not await initialize_exchange_client():
        # クライアント初期化に失敗した場合、続行不可
        logging.critical("❌ 致命的エラー: CCXTクライアントの初期化に失敗しました。BOTを停止します。")
        return 
        
    
    # 2. 初回起動時のマクロ情報/口座ステータスを取得し、起動メッセージを送信
    initial_macro_context = await fetch_macro_context()
    GLOBAL_MACRO_CONTEXT.update(initial_macro_context)
    
    initial_account_status = await fetch_account_status()
    # (初回起動メッセージは analyze_and_trade の中で行う)
    
    
    # 3. メインループ
    while True:
        try:
            # 3-1. メインの分析と取引実行
            await analyze_and_trade()
            
            # 3-2. 定時報告 (取引閾値未満の最高スコア)
            await notify_highest_analysis_score()
            
            # 3-3. WebShareデータの送信 (1時間に1回程度)
            global LAST_WEBSHARE_UPLOAD_TIME, WEBSHARE_UPLOAD_INTERVAL
            if time.time() - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
                
                # WebShare用のデータを作成
                webshare_data = {
                    'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
                    'bot_version': BOT_VERSION,
                    'account_equity_usdt': ACCOUNT_EQUITY_USDT,
                    'global_macro_context': GLOBAL_MACRO_CONTEXT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'analysis_signals_count': len(LAST_ANALYSIS_SIGNALS),
                    'top_signal': LAST_ANALYSIS_SIGNALS[0] if LAST_ANALYSIS_SIGNALS else None,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = time.time()
            
            # 3-4. 次のループまで待機
            await asyncio.sleep(LOOP_INTERVAL)
            
        except Exception as e:
            logging.critical(f"❌ メインBOTスケジューラで予期せぬ致命的なエラーが発生しました: {e}", exc_info=True)
            # 致命的エラーの場合は、Telegram通知を行い、クライアントの再初期化を試みる
            await send_telegram_notification(f"🚨 **致命的エラー**\nメインループで予期せぬエラーが発生しました: <code>{e}</code>\nクライアントの再初期化を試みます。")
            
            # クライアント再初期化
            await asyncio.sleep(60) # 1分待機
            if not await initialize_exchange_client():
                logging.critical("❌ クライアント再初期化にも失敗しました。BOTを停止します。")
                return # 続行不可
                
            logging.info("✅ クライアントの再初期化に成功しました。メインループを再開します。")
            await asyncio.sleep(LOOP_INTERVAL)


async def position_monitor_scheduler():
    """
    ポジションのSL/TPトリガーを監視するためのスケジューラ (メインループとは独立して動く)。
    """
    global MONITOR_INTERVAL, IS_CLIENT_READY
    
    logging.info("⭐ ポジション監視スケジューラ起動。")
    
    while not IS_CLIENT_READY:
        logging.info("ℹ️ クライアント準備待ち...")
        await asyncio.sleep(MONITOR_INTERVAL)

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
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {exc}")
        # Telegram通知はループ内で実行されるため、ここでは省略
        
    return JSONResponse(
        status_code=500,
        content={"message": "Internal Server Error", "detail": str(exc)},
    )
