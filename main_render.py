# ====================================================================================
# Apex BOT v20.0.47 - Future Trading / 30x Leverage 
# (Feature: 最高スコアの分析結果をログファイルに必ず記録)
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

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
# 🚨 注意: CCXTの標準シンボル形式 ('BTC/USDT') を使用
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
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)
BOT_VERSION = "v20.0.47"            # 💡 BOTバージョンを更新 (最高スコア分析ログ)
FGI_API_URL = "https://api.alternative.me/fng/?limit=1" # 💡 FGI API URL

LOOP_INTERVAL = 60 * 1              # メインループの実行間隔 (秒) - 1分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間間隔 (秒) - 10秒ごと

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 先物取引設定 
LEVERAGE = 30 # 取引倍率
TRADE_TYPE = 'future' # 取引タイプ
MIN_MAINTENANCE_MARGIN_RATE = 0.005 # 最低維持証拠金率 (例: 0.5%) - 清算価格計算に使用

# 💡 レートリミット対策用定数
LEVERAGE_SETTING_DELAY = 1.0 # レバレッジ設定時のAPIレートリミット対策用遅延 (秒)

# 💡 【固定ロット】設定 
# 🚨 リスクベースの動的サイジング設定は全て削除し、この固定値を使用します。
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
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)
ACCOUNT_EQUITY_USDT: float = 0.0 # 現時点での総資産 (リスク計算に使用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 12 
SIGNAL_THRESHOLD = 0.65             
TOP_SIGNAL_COUNT = 1                
# 💥 修正: 1mのOHLCVデータ要求を200に引き下げて、データ不足を回避
REQUIRED_OHLCV_LIMITS = {'1m': 200, '5m': 500, '15m': 500, '1h': 500, '4h': 500} 

# テクニカル分析定数 
# 💥 修正: 5mを削除
TARGET_TIMEFRAMES = ['1m', '15m', '1h', '4h'] 
BASE_SCORE = 0.40                  # 初期スコア
LONG_TERM_SMA_LENGTH = 200         
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ/一致時のボーナス
STRUCTURAL_PIVOT_BONUS = 0.05       # 構造的な優位性ボーナス (固定)
RSI_MOMENTUM_LOW = 40              # RSIモメンタム加速の閾値
MACD_CROSS_PENALTY = 0.15          # MACDモメンタム逆行時のペナルティ/一致時のボーナス
LIQUIDITY_BONUS_MAX = 0.06          # 流動性ボーナス
FGI_PROXY_BONUS_MAX = 0.05         # FGIマクロ要因最大影響度
FOREX_BONUS_MAX = 0.0               # 為替マクロ要因最大影響度 (未使用)

# 💎 新規追加: 高度分析用定数 
RSI_OVERBOUGHT_PENALTY = -0.12  # RSIが極端な水準にある場合の重大な減点
RSI_OVERSOLD_THRESHOLD = 30     # 買いシグナル時のRSI下限閾値
RSI_OVERBOUGHT_THRESHOLD = 70   # 売りシグナル時のRSI上限閾値

ADL_ACCUMULATION_BONUS = 0.08   # A/Dライン蓄積・分散ボーナス
ADX_TREND_STRENGTH_BONUS = 0.07 # ADXトレンド強度が強い場合の加点
ADX_STRENGTH_THRESHOLD = 25     # ADXがこの値を超えている場合にトレンド強しと判断


# ボラティリティ指標 (ATR) の設定 
ATR_LENGTH = 14
ATR_MULTIPLIER_SL = 2.0 # SLをATRの2.0倍に設定 (動的SLのベース)
MIN_RISK_PERCENT = 0.008 # SL幅の最小パーセンテージ (0.8%)

# 市場環境に応じた動的閾値調整のための定数
FGI_SLUMP_THRESHOLD = -0.02         
FGI_ACTIVE_THRESHOLD = 0.02         
SIGNAL_THRESHOLD_SLUMP = 0.95       
SIGNAL_THRESHOLD_NORMAL = 0.90      
SIGNAL_THRESHOLD_ACTIVE = 0.85      

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
        
    return max(0.0, liquidation_price) # 価格は0未満にはならない

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
        f"  - **取引ロット**: **固定** <code>{FIXED_NOTIONAL_USDT}</code> **USDT**\n" 
        f"  - **最大リスク/取引**: **固定ロット**のため動的設定なし\n" 
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
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_raw_value}</code> ({'リスクオン' if fgi_proxy > FGI_ACTIVE_THRESHOLD else ('リスクオフ' if fgi_proxy < FGI_SLUMP_THRESHOLD else '中立')})\n"
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
    
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0) if trade_result else 0.0)
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0) if trade_result else 0.0)
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0) if trade_result else 0.0)
    liquidation_price = signal.get('liquidation_price', 0.0) 
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) if context != "ポジション決済" else ""

    trade_section = ""
    trade_status_line = ""
    
    # リスク幅、リワード幅の計算をLong/Shortで反転
    risk_width = abs(entry_price - stop_loss)
    reward_width = abs(take_profit - entry_price)

    if context == "取引シグナル":
        # lot_size_units = signal.get('lot_size_units', 0.0) # 数量 (単位)
        notional_value = trade_result.get('filled_usdt', FIXED_NOTIONAL_USDT) # 実際に約定した名目価値
        
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
            # 💡 risk_usdt の計算は複雑なため、固定ロットベースで簡略化
            risk_percent = abs(entry_price - stop_loss) / entry_price
            risk_usdt = filled_usdt_notional * risk_percent * LEVERAGE # 簡易的なSLによる名目リスク
            
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>先物 (Future) / {order_type_text} ({side.capitalize()})</code>\n" 
                f"  - **レバレッジ**: <code>{LEVERAGE}</code> 倍\n" 
                f"  - **名目ロット**: <code>{format_usdt(filled_usdt_notional)}</code> USDT (固定)\n" 
                f"  - **推定リスク額**: <code>{format_usdt(risk_usdt)}</code> USDT (計算 SL: {risk_percent*100:.2f}%)\n"
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
            # 🚨 数量が0の場合のエラーを回避
            if filled_amount == 0.0:
                filled_amount = trade_result.get('contracts', 0.0)
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
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
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
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    # 💡 新しいログタイプのための処理
    if context == "最高スコア分析結果":
         message = (
            f"🔎 **最高スコア分析結果 (取引スキップ)**\n"
            f"  - **日時**: {now_jst} (JST)\n"
            f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
            f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
            f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点 (不足)\n"
            f"  - **サイド**: {side.capitalize()}\n"
            f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
            f"  - **RRR**: <code>1:{rr_ratio:.2f}</code>\n"
            f"  - **エントリー**: <code>{format_price(entry_price)}</code>\n"
            f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        # 最高スコアの通知は基本的にTelegramには送らないが、テスト用に残す
        # return message 

    
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
        # requestsをawait asyncio.to_threadで非同期実行
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
    # 1m分析でSMA200をスキップした場合、trend_valは0.0になるため、テキストを修正
    if signal['timeframe'] == '1m' and trend_val == 0.0:
        trend_text = "🟡 長期トレンド分析スキップ (1m軽量モード)"
        
    breakdown_list.append(f"{trend_text}: {trend_val*100:+.2f} 点")

    # MACDモメンタム
    macd_val = tech_data.get('macd_penalty_value', 0.0)
    macd_text = "🟢 MACDモメンタム一致" if macd_val > 0 else ("🔴 MACDモメンタム逆行" if macd_val < 0 else "🟡 MACDモメンタム中立")
    breakdown_list.append(f"{macd_text}: {macd_val*100:+.2f} 点")
    
    # RSIモメンタム
    rsi_val = tech_data.get('rsi_momentum_bonus_value', 0.0)
    if rsi_val > 0:
        breakdown_list.append(f"🟢 RSIモメンタム加速: {rsi_val*100:+.2f} 点")
    
    # OBV確証
    obv_val = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_val > 0:
        breakdown_list.append(f"🟢 OBV出来高確証: {obv_val*100:+.2f} 点")

    # 💎 新規追加: A/Dライン蓄積ボーナス
    adl_val = tech_data.get('adl_accumulation_bonus', 0.0)
    if adl_val > 0:
        adl_text = "🟢 A/Dライン蓄積/分散" 
        breakdown_list.append(f"{adl_text}: {adl_val*100:+.2f} 点")

    # 💎 新規追加: ADXトレンド強度ボーナス
    adx_val = tech_data.get('adx_trend_strength_bonus', 0.0)
    if adx_val > 0:
        breakdown_list.append(f"🟢 ADXトレンド確証: {adx_val*100:+.2f} 点")

    # 流動性ボーナス
    liq_val = tech_data.get('liquidity_bonus_value', 0.0)
    if liq_val > 0:
        breakdown_list.append(f"🟢 流動性 (TOP銘柄): {liq_val*100:+.2f} 点")
        
    # FGIマクロ影響
    fgi_val = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_text = "🟢 FGIマクロ追い風" if fgi_val > 0 else ("🔴 FGIマクロ向かい風" if fgi_val < 0 else "🟡 FGIマクロ中立")
    breakdown_list.append(f"{fgi_text}: {fgi_val*100:+.2f} 点")
    
    # 構造的ボーナス
    struct_val = tech_data.get('structural_pivot_bonus', 0.0)
    breakdown_list.append(f"🟢 構造的優位性 (ベース): {struct_val*100:+.2f} 点")

    # ペナルティ要因の表示
    
    # ボラティリティペナルティ
    vol_val = tech_data.get('volatility_penalty_value', 0.0)
    if vol_val < 0:
        breakdown_list.append(f"🔴 ボラティリティ過熱P: {vol_val*100:+.2f} 点")
        
    # 💎 新規追加: RSI過熱反転ペナルティ
    rsi_over_val = tech_data.get('rsi_overbought_penalty_value', 0.0)
    if rsi_over_val < 0:
        breakdown_list.append(f"🔴 RSI過熱反転P: {rsi_over_val*100:+.2f} 点")
        
    
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
            'trade_result': trade_result or data.get('trade_result', None),
            'full_data': data,
        }
        
        cleaned_log_entry = _to_json_compatible(log_entry)

        # 💡 ログタイプに応じてファイル名を決定 (最高スコア分析結果は専用のログファイルに)
        if log_type == "最高スコア分析結果":
            log_file = "apex_bot_top_analysis_log.jsonl"
        else:
            log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
            
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


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
            
            # requestsをawait asyncio.to_threadで非同期実行
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
            
            # --- Patch 70 FIX 終了 ---

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
            balance = await EXCHANGE_CLIENT.fetch_balance(params={'defaultType': 'swap'})
        else:
            fetch_params = {'type': 'future'} if TRADE_TYPE == 'future' else {}
            balance = await EXCHANGE_CLIENT.fetch_balance(params=fetch_params)

        if not balance:
            raise Exception("Balance object is empty.")
            
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0) 

        # 2. MEXC特有のフォールバックロジック (infoからtotalEquityを探す)
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
                            
                if total_usdt_balance_fallback > 0:
                    total_usdt_balance = total_usdt_balance_fallback
                    logging.warning("⚠️ MEXC専用フォールバックロジックで Equity を取得しました。")

        ACCOUNT_EQUITY_USDT = total_usdt_balance
        
        return {
            'total_usdt_balance': total_usdt_balance,
            'open_positions': [], # ポジションはfetch_open_positionsで取得するためここでは空
            'error': False
        }
    except ccxt.NetworkError as e:
        logging.error(f"❌ 口座ステータス取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ 口座ステータス取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return {'total_usdt_balance': 0.0, 'open_positions': [], 'error': True}

async def fetch_open_positions() -> List[Dict]:
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        logging.error("❌ ポジション取得失敗: CCXTクライアントが準備できていません。")
        return []

    try:
        if EXCHANGE_CLIENT.has['fetchPositions']:
            positions_ccxt = await EXCHANGE_CLIENT.fetch_positions()
        else:
            logging.error("❌ ポジション取得失敗: 取引所が fetch_positions APIをサポートしていません。")
            return []

        new_open_positions = []
        for p in positions_ccxt:
            if p and p.get('symbol') and p.get('contracts', 0) != 0:
                # ユーザーが監視対象としている銘柄のみを抽出 (シンボル形式が一致することを前提)
                if p['symbol'] in CURRENT_MONITOR_SYMBOLS:
                    side = 'short' if p['contracts'] < 0 else 'long'
                    entry_price = p.get('entryPrice')
                    contracts = abs(p['contracts'])
                    notional_value = p.get('notional')
                    
                    if entry_price is None or notional_value is None:
                        logging.warning(f"⚠️ {p['symbol']} のポジション情報が不完全です。スキップします。")
                        continue
                        
                    new_open_positions.append({
                        'symbol': p['symbol'],
                        'side': side,
                        'entry_price': entry_price,
                        'contracts': contracts,
                        'filled_usdt': notional_value,
                        'timestamp': p.get('timestamp', time.time() * 1000),
                        'stop_loss': 0.0, # SL/TPは後のロジックで同期されることを想定
                        'take_profit': 0.0,
                    })

        OPEN_POSITIONS = new_open_positions
        
        # ログ強化ポイント: ポジション数が0の場合のログをより明示的に
        if len(OPEN_POSITIONS) == 0:
            logging.info("✅ CCXTから最新のオープンポジション情報を取得しました (現在 0 銘柄)。 **(ポジション不在)**")
        else:
            logging.info(f"✅ CCXTから最新のオープンポジション情報を取得しました (現在 {len(OPEN_POSITIONS)} 銘柄)。")
            
        return OPEN_POSITIONS
    except ccxt.NetworkError as e:
        logging.error(f"❌ ポジション取得失敗 (ネットワークエラー): {e}")
    except ccxt.AuthenticationError as e:
        logging.critical(f"❌ ポジション取得失敗 (認証エラー): APIキー/シークレットを確認してください。{e}")
    except Exception as e:
        logging.error(f"❌ ポジション取得失敗 (予期せぬエラー): {e}", exc_info=True)
        
    return []

# ====================================================================================
# CORE LOGIC: TECHNICAL ANALYSIS & SCORING (NEW V20.0.47 INTEGRATION)
# ====================================================================================

# ------------------------------------------------
# 1. 総合スコア集計ロジック
# ------------------------------------------------
def calculate_signal_score(signal: Dict[str, Any]) -> float:
    """
    シグナル詳細分析の各要素を合計し、最終的な総合スコアを計算する。
    """
    
    tech_data = signal.get('tech_data', {})
    
    # 1. ベーススコアから開始
    score = BASE_SCORE # 0.40
    
    # 2. 加点・減点の要素を集計
    
    # トレンド・構造的要因
    score += tech_data.get('structural_pivot_bonus', 0.0)
    score += tech_data.get('long_term_reversal_penalty_value', 0.0)
    
    # モメンタム・出来高要因
    score += tech_data.get('macd_penalty_value', 0.0)
    score += tech_data.get('rsi_divergence_bonus_value', 0.0) 
    score += tech_data.get('obv_momentum_bonus_value', 0.0) 
    
    # 💎 新規追加: 高度分析要素
    score += tech_data.get('adl_accumulation_bonus', 0.0)
    score += tech_data.get('adx_trend_strength_bonus', 0.0)
    score += tech_data.get('rsi_overbought_penalty_value', 0.0)
    
    # マクロ・流動性要因
    score += tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    score += tech_data.get('liquidity_bonus_value', 0.0)
    score += tech_data.get('forex_bonus_value', 0.0) # 既存コード要素
    
    # ペナルティ要因 (既存)
    score += tech_data.get('volatility_penalty_value', 0.0)
    
    # 3. スコアの値を丸め、上限を設定
    final_score = round(score, 4)
    # スコアは0未満にならない
    final_score = max(0.0, final_score) 
    
    # 総合スコアをシグナル辞書に保存
    signal['score'] = final_score
    
    return final_score


# ------------------------------------------------
# 2. 実戦的分析ロジック
# ------------------------------------------------
def calculate_advanced_analysis(df: pd.DataFrame, signal: Dict[str, Any], signal_type: str):
    """
    データフレームに基づき、高度なテクニカル分析を行い、
    スコア計算用の加点・減点値をシグナル辞書の 'tech_data' に設定する。
    """
    
    # 最終行のデータを取得
    if df.empty:
        logging.error("分析用のデータフレームが空です。")
        return
        
    last_row = df.iloc[-1]
    
    # tech_dataを初期化
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    current_close = last_row['Close']
    
    # 1. 既存の分析項目
    
    # Long Term Reversal (長期トレンド: SMA200との比較)
    # 200SMA計算に必要なデータが揃っているか確認
    tech_data['long_term_reversal_penalty_value'] = 0.0
    
    if f'SMA_{LONG_TERM_SMA_LENGTH}' not in df.columns:
        # SMA200が計算できていない場合は、トレンド要因を中立（0.0）とする
        long_term_sma = current_close
        if timeframe != '1m':
             logging.warning(f"SMA_{LONG_TERM_SMA_LENGTH} が計算されていません。トレンド要因をスキップします。")
        # 1mの場合は意図的なスキップなので警告を省略
    else:
        long_term_sma = last_row[f'SMA_{LONG_TERM_SMA_LENGTH}']
        
        if long_term_sma != current_close: # スキップの場合を除外
            if signal_type == 'long' and current_close > long_term_sma:
                tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY 
            elif signal_type == 'short' and current_close < long_term_sma:
                tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY 
            elif signal_type == 'long' and current_close < long_term_sma:
                tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY 
            elif signal_type == 'short' and current_close > long_term_sma:
                tech_data['long_term_reversal_penalty_value'] = -LONG_TERM_REVERSAL_PENALTY 
        
    # MACDモメンタム (MACDヒストグラムの方向性)
    macd_h = last_row.get('MACDh_12_26_9', 0.0)
    tech_data['macd_penalty_value'] = 0.0
    
    if signal_type == 'long' and macd_h > 0:
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY 
    elif signal_type == 'short' and macd_h < 0:
        tech_data['macd_penalty_value'] = MACD_CROSS_PENALTY 
    elif signal_type == 'long' and macd_h < 0:
        tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY 
    elif signal_type == 'short' and macd_h > 0:
        tech_data['macd_penalty_value'] = -MACD_CROSS_PENALTY 
        
    # 構造的優位性 (SMA50との比較)
    if 'SMA_50' in df.columns: # SMA_50は1mでも計算される
        sma_50 = last_row['SMA_50']
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS if (signal_type == 'long' and current_close > sma_50) or (signal_type == 'short' and current_close < sma_50) else 0.0
    else:
        # SMA_50すら計算できない場合はベースボーナスを固定で付与
        logging.warning(f"SMA_50が計算されていません。構造的優位性ボーナスを固定値 {STRUCTURAL_PIVOT_BONUS} に設定。")
        tech_data['structural_pivot_bonus'] = STRUCTURAL_PIVOT_BONUS

    # 流動性ボーナス (暫定的にTOP銘柄の有無で判断)
    TOP_SYMBOLS_FOR_LIQUIDITY = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"] 
    tech_data['liquidity_bonus_value'] = LIQUIDITY_BONUS_MAX if signal['symbol'] in TOP_SYMBOLS_FOR_LIQUIDITY else 0.0
    
    # FGIマクロ要因 (グローバル変数から取得)
    fgi_proxy = GLOBAL_MACRO_CONTEXT.get('fgi_proxy', 0.0)
    tech_data['sentiment_fgi_proxy_bonus'] = 0.0
    if signal_type == 'long' and fgi_proxy > 0:
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy
    elif signal_type == 'short' and fgi_proxy < 0:
        tech_data['sentiment_fgi_proxy_bonus'] = -fgi_proxy
    else:
        tech_data['sentiment_fgi_proxy_bonus'] = fgi_proxy 
    
    # ダミー/既存の要素 (出来高、ダイバージェンス、ボラティリティペナルティ)
    tech_data['rsi_divergence_bonus_value'] = RSI_DIVERGENCE_BONUS if random.random() < 0.3 else 0.0
    tech_data['obv_momentum_bonus_value'] = OBV_MOMENTUM_BONUS if random.random() < 0.5 else 0.0
    
    # ボラティリティペナルティ (BBandsの%Bを使用)
    tech_data['volatility_penalty_value'] = 0.0
    if 'BBP_5_2.0' in last_row:
        bbp = last_row['BBP_5_2.0']
        if bbp > 1.05 or bbp < -0.05:
            tech_data['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD 
    else:
        tech_data['volatility_penalty_value'] = -VOLATILITY_BB_PENALTY_THRESHOLD if random.random() < 0.2 else 0.0
        
    tech_data['forex_bonus_value'] = 0.0 # 未使用

    # 2. 💎 新規追加: 高度分析要素の計算
    
    # RSI過熱反転ペナルティの計算
    rsi = last_row.get('RSI_14', 50.0)
    tech_data['rsi_overbought_penalty_value'] = 0.0
    
    if signal_type == 'long' and rsi > RSI_OVERBOUGHT_THRESHOLD: 
        tech_data['rsi_overbought_penalty_value'] = RSI_OVERBOUGHT_PENALTY 
            
    elif signal_type == 'short' and rsi < RSI_OVERSOLD_THRESHOLD: 
        tech_data['rsi_overbought_penalty_value'] = RSI_OVERBOUGHT_PENALTY 

    # A/Dライン蓄積ボーナスの計算
    adl_diff = 0
    tech_data['adl_accumulation_bonus'] = 0.0
    if 'AD' in df.columns and len(df) >= 14:
        # 直近14期間のA/Dラインの変化
        adl_diff = last_row['AD'] - df['AD'].iloc[-14] 
        
        if signal_type == 'long' and adl_diff > 0:
            tech_data['adl_accumulation_bonus'] = ADL_ACCUMULATION_BONUS 
                
        elif signal_type == 'short' and adl_diff < 0:
            tech_data['adl_accumulation_bonus'] = ADL_ACCUMULATION_BONUS 

    # ADXトレンド強度ボーナスの計算
    tech_data['adx_trend_strength_bonus'] = 0.0
    if 'ADX_14' in df.columns:
        adx = last_row['ADX_14']
        dmi_plus = last_row.get('DMP_14', 0.0)
        dmi_minus = last_row.get('DMN_14', 0.0)

        if adx > ADX_STRENGTH_THRESHOLD: # ADX > 25 (トレンドが強い)
            if signal_type == 'long' and dmi_plus > dmi_minus:
                tech_data['adx_trend_strength_bonus'] = ADX_TREND_STRENGTH_BONUS 
                
            elif signal_type == 'short' and dmi_minus > dmi_plus:
                tech_data['adx_trend_strength_bonus'] = ADX_TREND_STRENGTH_BONUS 

    # tech_dataをシグナル辞書に戻す
    signal['tech_data'] = tech_data


async def fetch_and_analyze(exchange: ccxt_async.Exchange, symbol: str, timeframe: str) -> List[Dict[str, Any]]:
    """
    OHLCVデータを取得し、テクニカル分析を行い、シグナルスコアを計算する。
    """
    
    # 💥 修正: 設定されたOHLCVの最大取得期間を使用
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
    
    try:
        # 1. データの取得
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=limit) 
        
        # ATR/SMA計算に必要な期間（例: 200期間）のチェックは、1m軽量化のためスキップ
        if len(ohlcv) < ATR_LENGTH + 50: # SMA50とATR14の計算に必要な最低限のデータ
            logging.warning(f"データ不足: {symbol} - {timeframe} ({len(ohlcv)}期間)。SMA50とATR14の計算に必要なデータが不足しています。")
            return []
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        
        # 2. テクニカル指標の計算 (pandas-taを使用)
        df.ta.ema(length=20, append=True) 
        df.ta.macd(append=True)
        df.ta.rsi(append=True)
        
        # 💥 修正: 1mはSMA200をスキップしてデータ不足エラーを解消
        if timeframe != '1m':
            df.ta.sma(length=LONG_TERM_SMA_LENGTH, append=True) # 200SMA (1m以外)
            
        df.ta.sma(length=50, append=True) # 50SMA (全てのTFで使用)
        
        df.ta.obv(append=True) # OBV (出来高)
        df.ta.bbands(append=True) # BBands (ボラティリティ)
        
        # 💎 新規追加: A/DラインとADX/DMIの計算
        df.ta.ad(append=True) # A/Dライン
        df.ta.adx(append=True) # ADX/DMI
        
        # 💥 ATR の計算を追加 
        df.ta.atr(length=ATR_LENGTH, append=True) # ATR_14を追加
        
        # データが NaN を含む行を削除 (テクニカル指標の計算に必要なデータが揃うまで待つ)
        df = df.dropna()
        if df.empty or len(df) < 5:
            logging.warning(f"{symbol} のテクニカル指標計算後、データが不足しました。")
            return []
            
        # 3. シグナル判定 (最後のMACDクロス方向をシグナルとする)
        # dropona()によってMACDカラムが存在することが前提となる
        last_macd_h = df['MACDh_12_26_9'].iloc[-1]
        prev_macd_h = df['MACDh_12_26_9'].iloc[-2]
        
        signals: List[Dict[str, Any]] = []
        
        # MACDゴールデンクロス -> Longシグナル
        if last_macd_h > 0 and prev_macd_h <= 0:
            signals.append({'symbol': symbol, 'side': 'long', 'timeframe': timeframe, 'entry_price': df['Close'].iloc[-1]})
        
        # MACDデッドクロス -> Shortシグナル
        if last_macd_h < 0 and prev_macd_h >= 0:
            signals.append({'symbol': symbol, 'side': 'short', 'timeframe': timeframe, 'entry_price': df['Close'].iloc[-1]})
        
        if not signals:
            return []
            
        # 4. 🚀 高度なテクニカル分析とスコアリングの実行
        final_signals = []
        
        # 💥 修正点: ATR列の存在チェックを厳密化
        atr_column_name = f'ATR_{ATR_LENGTH}'
        
        # ATRが存在しない場合は、データが完全に不足しているため、スキップ
        if atr_column_name not in df.columns or df[atr_column_name].iloc[-1] is None or pd.isna(df[atr_column_name].iloc[-1]):
            # 🚨 SMA200依存を解消したため、ここでエラーになる場合はデータ自体が14本も取得できていない極端なケース
            logging.warning(f"⚠️ {symbol} - {timeframe} の分析をスキップ: ATRデータ '{atr_column_name}' の最終値が計算されていません。データが極度に不足しています。")
            return [] 
            
        last_row = df.iloc[-1]
        
        for signal in signals:
            
            # 4-1. 💡 高度なテクニカル分析を実行し、tech_dataに加点/減点要素を書き込む
            calculate_advanced_analysis(df, signal, signal['side'])
            
            # 4-2. 💰 総合スコアの計算
            final_score = calculate_signal_score(signal)
            
            # 4-3. リスク/リワードの計算 (ATRを使用して動的に計算)
            # ATRの存在は既にチェック済み
            atr = last_row[atr_column_name] 
            entry_price = signal['entry_price']
            
            # ATRベースのリスク幅
            risk_amount = atr * ATR_MULTIPLIER_SL
            
            if signal['side'] == 'long':
                stop_loss = entry_price - risk_amount
                # RRR 1:2 を想定
                take_profit = entry_price + (risk_amount * 2.0) 
            else:
                stop_loss = entry_price + risk_amount
                take_profit = entry_price - (risk_amount * 2.0) 
                
            # 清算価格の計算
            liquidation_price = calculate_liquidation_price(
                entry_price, LEVERAGE, signal['side'], MIN_MAINTENANCE_MARGIN_RATE
            )
            
            # リスクリワード比率
            sl_abs = abs(entry_price - stop_loss)
            tp_abs = abs(take_profit - entry_price)
            rr_ratio = round(tp_abs / sl_abs, 2) if sl_abs > 0 else 0.0
            
            # 最小リスク SL% のチェック (リジェクトロジックを削除し、データとして保持)
            risk_percent = sl_abs / entry_price if entry_price > 0 else 0.0
            
            # 結果をシグナル辞書に追加
            signal.update({
                'score': final_score,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'liquidation_price': liquidation_price,
                'rr_ratio': rr_ratio,
                'risk_percent': risk_percent,
                'is_low_volatility_reject': risk_percent < MIN_RISK_PERCENT, # データとして残す
            })
            
            # RSIモメンタム加速ボーナス（分析完了後、RSI値に依存する加点ロジック）
            rsi = last_row.get('RSI_14', 50.0)
            rsi_momentum_bonus_value = 0.0
            
            if signal['side'] == 'long' and rsi > RSI_MOMENTUM_LOW:
                rsi_momentum_bonus_value = (rsi - RSI_MOMENTUM_LOW) / (100 - RSI_MOMENTUM_LOW) * 0.04 # 最大 4%
            elif signal['side'] == 'short' and rsi < 100 - RSI_MOMENTUM_LOW:
                rsi_momentum_bonus_value = ((100 - RSI_MOMENTUM_LOW) - rsi) / (100 - RSI_MOMENTUM_LOW) * 0.04 # 最大 4%
                
            signal['tech_data']['rsi_momentum_bonus_value'] = rsi_momentum_bonus_value
            # 総合スコアを再度更新
            calculate_signal_score(signal)
            
            final_signals.append(signal)
            
        return final_signals

    except ccxt.NetworkError as e:
        logging.error(f"❌ OHLCVデータ取得失敗 (ネットワークエラー): {symbol} - {timeframe}: {e}")
    except ccxt.DDoSProtection as e:
        logging.warning(f"⚠️ {symbol} - {timeframe} の分析をスキップ: DDoS対策によるレートリミット: {e}")
    except Exception as e:
        logging.error(f"❌ 予期せぬ分析エラー: {symbol} - {timeframe}: {e}", exc_info=True)
        
    return []

# ------------------------------------------------
# 3. 実行関数
# ------------------------------------------------

async def run_analysis(exchange: ccxt_async.Exchange, symbols: List[str], timeframes: List[str]) -> List[Dict[str, Any]]:
    """
    全シンボルとタイムフレームで並行して分析を実行し、有効なシグナルを全て返す。
    """
    
    tasks = []
    for symbol in symbols:
        for tf in timeframes:
            tasks.append(fetch_and_analyze(exchange, symbol, tf))
            
    # 全ての分析タスクを並行で実行
    results = await asyncio.gather(*tasks)
    
    # 結果リストをフラット化 (Noneや空リストを除去)
    all_signals = [signal for sublist in results if sublist for signal in sublist]
    
    # スコア順にソート（最高スコアを上位に）
    all_signals.sort(key=lambda x: x['score'], reverse=True)
    
    logging.info(f"✅ 全分析を完了しました。合計 {len(all_signals)} 件のシグナルを検出しました。")
    
    return all_signals

async def execute_trade(signal: Dict) -> Optional[Dict]:
    """
    シグナルに基づき、実際に取引を執行する (成行注文 + OCO注文設定)。
    """
    global EXCHANGE_CLIENT, OPEN_POSITIONS
    
    if TEST_MODE:
        logging.warning(f"⚠️ TEST_MODE: 取引実行をスキップします。シグナル: {signal['symbol']} {signal['side']}")
        return {'status': 'ok', 'filled_amount': 0.0, 'filled_usdt': FIXED_NOTIONAL_USDT, 'entry_price': signal['entry_price']}


    if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
        return {'status': 'error', 'error_message': 'CCXTクライアントが準備できていません。'}
        
    symbol = signal['symbol']
    side = signal['side']
    entry_price = signal['entry_price']
    stop_loss = signal['stop_loss']
    take_profit = signal['take_profit']
    
    # 既存ポジションチェック (ロジックを簡素化し、ここでは排他制御をしない)
    
    try:
        # 1. ロットサイズの計算 (固定ロットを使用)
        # 固定名目ロットをエントリー価格で割って注文数量を決定
        # 数量 = 名目ロット / 価格
        amount_raw = FIXED_NOTIONAL_USDT * LEVERAGE / entry_price 
        
        # 最小取引量 (min_amount) の取得と数量の丸め
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            raise Exception(f"市場情報が見つかりません: {symbol}")

        price_precision = market['precision']['price']
        amount_precision = market['precision']['amount']
        
        # 数量を取引所の精度で丸める (ここではccxtのround_to_precisionに相当する処理をPythonのroundで簡易実行)
        amount = round(amount_raw, amount_precision)
        
        # min_amountチェック
        min_amount = market.get('limits', {}).get('amount', {}).get('min', 0.0)
        if amount < min_amount:
            logging.error(f"❌ {symbol} の計算ロット ({amount:.4f}) が最小取引量 ({min_amount}) を下回りました。取引をスキップ。")
            return {'status': 'error', 'error_message': 'ロットが小さすぎます。'}
        
        # 注文サイドとタイプの設定
        order_side = 'buy' if side == 'long' else 'sell'
        
        # 2. メインの成行注文を実行
        order = await EXCHANGE_CLIENT.create_order(
            symbol, 
            'market', 
            order_side, 
            amount, 
            params={'leverage': LEVERAGE} # MEXCでレバレッジを再設定
        )

        filled_amount = order.get('filled', amount)
        final_entry_price = order.get('price', entry_price) 
        
        # 3. OCO (SL/TP) 注文の設定
        
        # 利確/損切りのサイドはメイン注文の逆
        oco_side = 'sell' if side == 'long' else 'buy'
        
        # 損切りの価格を精度で丸める
        sl_price = round(stop_loss, price_precision)
        
        # 利確の価格を精度で丸める
        tp_price = round(take_profit, price_precision)

        # OCO注文を送信 (CCXTのcreate_orderにStop/Limitの機能があるか、またはcreate_oco_orderを使用)
        # 🚨 MEXCの場合、TP/SLは set_trading_stop で設定する必要がある
        
        if EXCHANGE_CLIENT.id == 'mexc':
            # MEXC: set_trading_stop (TP/SL)
            # 常に全保有量に対して設定する
            # stopLossPrice: 損切り価格
            # takeProfitPrice: 利確価格
            
            # price_precisionで丸める
            params = {
                'stopLossPrice': sl_price, 
                'takeProfitPrice': tp_price
            }
            
            try:
                # 損切りと利確を同時に設定
                await EXCHANGE_CLIENT.set_trading_stop(
                    symbol, 
                    amount=filled_amount, # ポジションサイズ全体に対して設定
                    side=oco_side,        # 決済サイド
                    params=params
                )
                logging.info(f"✅ {symbol} OCO注文 (TP:{tp_price}, SL:{sl_price}) を設定しました。")
            except Exception as e:
                logging.error(f"❌ {symbol} OCO注文設定に失敗: {e}")
                
        else:
            # 他の取引所向け: Stop Market/Limit注文を直接送信するロジック (ここでは省略)
             logging.warning(f"⚠️ {EXCHANGE_CLIENT.id} の OCO/Trading Stop ロジックは未実装です。手動で設定してください。")


        # 4. ポジションリストの更新 (SL/TP情報を含める)
        OPEN_POSITIONS.append({
            'symbol': symbol,
            'side': side,
            'entry_price': final_entry_price,
            'contracts': filled_amount,
            'filled_usdt': final_entry_price * filled_amount / LEVERAGE * LEVERAGE, # 名目価値 (近似)
            'timestamp': int(time.time() * 1000),
            'stop_loss': sl_price,
            'take_profit': tp_price,
        })
        
        return {
            'status': 'ok', 
            'filled_amount': filled_amount, 
            'filled_usdt': final_entry_price * filled_amount / LEVERAGE * LEVERAGE,
            'entry_price': final_entry_price,
            'stop_loss': sl_price,
            'take_profit': tp_price
        }

    except ccxt.InsufficientFunds as e:
        logging.error(f"❌ 取引失敗 (残高不足): {symbol}: {e}")
        return {'status': 'error', 'error_message': '残高不足'}
    except ccxt.InvalidOrder as e:
        logging.error(f"❌ 取引失敗 (無効な注文): {symbol}: {e}")
        return {'status': 'error', 'error_message': f'無効な注文: {e}'}
    except Exception as e:
        logging.error(f"❌ 取引実行中に予期せぬエラー: {symbol}: {e}", exc_info=True)
        return {'status': 'error', 'error_message': f'予期せぬエラー: {e}'}

# ------------------------------------------------
# 4. マクロ要因の取得
# ------------------------------------------------

async def fetch_fgi_score():
    """Fear & Greed Index (FGI) を取得し、マクロコンテキストを更新する。"""
    global GLOBAL_MACRO_CONTEXT
    
    try:
        # requestsをawait asyncio.to_threadで非同期実行
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data and 'data' in data and data['data']:
            fgi_value = int(data['data'][0]['value'])
            fgi_classification = data['data'][0]['value_classification']
            
            # FGIスコアを -0.05 (Fear) から +0.05 (Greed) の範囲に正規化
            # (0-100 を -50 から +50 に変換し、1000で割る)
            # FGI=0(Extreme Fear) -> -0.05
            # FGI=50(Neutral) -> 0.00
            # FGI=100(Extreme Greed) -> +0.05
            fgi_proxy = (fgi_value - 50) / 1000 * FGI_PROXY_BONUS_MAX * 20 # *20で最大0.05
            
            GLOBAL_MACRO_CONTEXT['fgi_proxy'] = fgi_proxy
            GLOBAL_MACRO_CONTEXT['fgi_raw_value'] = f"{fgi_value} ({fgi_classification})"
            
            logging.info(f"FGIスコアを更新: FGI={fgi_value}, スコア影響度={fgi_proxy:.4f}")
            
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ FGI APIからデータを取得できませんでした: {e}")
    except Exception as e:
        logging.error(f"❌ FGIデータの処理中にエラーが発生: {e}", exc_info=True)

# ------------------------------------------------
# 5. メインループとスケジューラ
# ------------------------------------------------

async def main_bot_loop():
    """
    ボットのメイン処理ループ: 状況取得、分析、シグナル選定、取引実行。
    """
    global IS_FIRST_MAIN_LOOP_COMPLETED, LAST_ANALYSIS_SIGNALS
    
    logging.info("--- 新しいメインループを開始します ---")
    
    # 1. マクロ要因とアカウントステータスの更新
    await fetch_fgi_score() 
    await fetch_open_positions() 
    
    account_status = await fetch_account_status()
    if account_status.get('error'):
        logging.critical("❌ アカウントステータス取得に失敗しました。取引をスキップします。")
        return

    # 2. 取引閾値の決定
    current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)

    # 3. 全銘柄の分析実行
    LAST_ANALYSIS_SIGNALS = await run_analysis(EXCHANGE_CLIENT, CURRENT_MONITOR_SYMBOLS, TARGET_TIMEFRAMES)

    # 4. 💡 【V20.0.47 変更点】最高スコアシグナルをログに記録 (取引実行前)
    if LAST_ANALYSIS_SIGNALS:
        # スコアで降順ソートされていることを確認
        top_signal = LAST_ANALYSIS_SIGNALS[0]
        
        # 最高スコアのシグナルを、スコアに関係なくログに記録
        log_signal(top_signal, "最高スコア分析結果")
        logging.info(f"✅ 最高スコア分析結果をログに記録しました: {top_signal['symbol']} ({top_signal['timeframe']}, Score: {top_signal['score']:.4f})")


    # 5. 取引シグナルの選別と実行
    
    # 既にポジションがある銘柄を除外
    open_symbols = {p['symbol'] for p in OPEN_POSITIONS}
    
    # 実行可能なシグナルを選別
    executable_signals = []
    
    for signal in LAST_ANALYSIS_SIGNALS:
        symbol = signal['symbol']
        
        # 閾値チェック
        if signal['score'] < current_threshold:
            continue
            
        # クールダウンチェック
        if current_time := LAST_SIGNAL_TIME.get(symbol):
            if time.time() - current_time < TRADE_SIGNAL_COOLDOWN:
                logging.info(f"⏭️ {symbol} はクールダウン中 (次回実行: {datetime.fromtimestamp(current_time + TRADE_SIGNAL_COOLDOWN, JST).strftime('%H:%M:%S')})")
                continue
                
        # ポジションの排他チェック
        if symbol in open_symbols:
            logging.info(f"⏭️ {symbol} は既にポジションを保有しているため、取引をスキップします。")
            continue
            
        executable_signals.append(signal)

    # 実行可能なシグナルのうち、TOP_SIGNAL_COUNT 件を実行
    executed_count = 0
    for signal in executable_signals[:TOP_SIGNAL_COUNT]:
        
        # 実行
        trade_result = await execute_trade(signal)
        
        # ログと通知
        if trade_result and trade_result.get('status') == 'ok':
            # 成功時のみクールダウンを更新
            LAST_SIGNAL_TIME[signal['symbol']] = time.time()
            executed_count += 1
            # 注文成功ログは trade_result に entry_price, sl_price, tp_price が含まれるべき
            
            # Telegram通知の送信
            telegram_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
            await send_telegram_notification(telegram_message)
            
            # ログファイルへの記録
            log_signal(signal, "取引シグナル", trade_result)
            
        elif trade_result and trade_result.get('status') == 'error':
             telegram_message = format_telegram_message(signal, "取引シグナル", current_threshold, trade_result)
             await send_telegram_notification(telegram_message)
             
        # 連続したAPIリクエストを避けるための遅延
        await asyncio.sleep(LEVERAGE_SETTING_DELAY) 


    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        # 初回起動通知
        initial_message = format_startup_message(account_status, GLOBAL_MACRO_CONTEXT, len(CURRENT_MONITOR_SYMBOLS), current_threshold)
        await send_telegram_notification(initial_message)
        IS_FIRST_MAIN_LOOP_COMPLETED = True
        logging.info("✅ BOTの最初のメイン分析ループが完了しました。")

    
async def position_monitor_loop():
    """
    ポジションのSL/TP監視および決済処理ループ
    """
    global OPEN_POSITIONS
    
    if not OPEN_POSITIONS:
        logging.debug("ポジション監視: 対象ポジションなし。")
        return

    # 1. 現在価格の取得 (ここでは簡略化のため、全ポジションのシンボルの価格を取得するロジックを想定)
    # 実際のCCXTでは、fetch_ticker や fetch_tickers を使用
    current_prices = {} 
    
    symbols_to_fetch = {p['symbol'] for p in OPEN_POSITIONS}
    
    try:
        # 例: fetch_tickers を使用して一括取得
        tickers = await EXCHANGE_CLIENT.fetch_tickers(list(symbols_to_fetch))
        current_prices = {s: t['last'] for s, t in tickers.items() if t and t.get('last')}
    except Exception as e:
        logging.error(f"❌ ポジション監視中の価格取得エラー: {e}")
        return

    positions_to_remove = []
    
    for pos in OPEN_POSITIONS:
        symbol = pos['symbol']
        side = pos['side']
        sl_price = pos['stop_loss']
        tp_price = pos['take_profit']
        entry_price = pos['entry_price']
        contracts = pos['contracts']
        
        current_price = current_prices.get(symbol)
        if not current_price:
            logging.warning(f"⚠️ {symbol} の現在価格を取得できませんでした。監視をスキップ。")
            continue
            
        exit_triggered = False
        exit_type = ""
        
        # 損切り判定 (SL)
        if side == 'long' and current_price <= sl_price:
            exit_triggered = True
            exit_type = "SL (ストップロス)"
        elif side == 'short' and current_price >= sl_price:
            exit_triggered = True
            exit_type = "SL (ストップロス)"
            
        # 利確判定 (TP) - SLが先にトリガーされていない場合
        elif side == 'long' and current_price >= tp_price:
            exit_triggered = True
            exit_type = "TP (テイクプロフィット)"
        elif side == 'short' and current_price <= tp_price:
            exit_triggered = True
            exit_type = "TP (テイクプロフィット)"

        if exit_triggered:
            # 6. ポジションの決済 (close_position)
            logging.info(f"🔥 {symbol} - {side.upper()} ポジション決済トリガー: {exit_type} 価格: {format_price(current_price)}")
            
            # 決済注文の実行 (ここではCCXTのclose_position関数を模倣)
            if not TEST_MODE:
                close_side = 'sell' if side == 'long' else 'buy'
                try:
                    # 決済処理の実行（成行で全量決済）
                    close_order = await EXCHANGE_CLIENT.create_order(
                        symbol, 
                        'market', 
                        close_side, 
                        contracts, 
                        params={'positionSide': side.capitalize()} # 必要に応じてポジションサイドを指定
                    )
                    
                    # 決済結果の計算
                    exit_price = current_price # 厳密には約定価格
                    pnl_usdt = contracts * (exit_price - entry_price) * LEVERAGE * (1 if side == 'long' else -1) # 簡易PNL
                    pnl_rate = (exit_price / entry_price - 1) * LEVERAGE * (1 if side == 'long' else -1) # 簡易PNL率
                    
                    trade_result = {
                        'status': 'closed',
                        'exit_type': exit_type,
                        'exit_price': exit_price,
                        'entry_price': entry_price,
                        'pnl_usdt': pnl_usdt,
                        'pnl_rate': pnl_rate,
                        'filled_amount': contracts,
                    }
                    
                    # Telegram通知
                    telegram_message = format_telegram_message(pos, "ポジション決済", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
                    await send_telegram_notification(telegram_message)

                    # ログファイルへの記録
                    log_signal(pos, "ポジション決済", trade_result)
                    
                    # 決済されたポジションをリストから削除
                    positions_to_remove.append(pos)
                    
                except Exception as e:
                    logging.error(f"❌ {symbol} のポジション決済注文失敗: {e}", exc_info=True)
            else:
                 # テストモードの模擬決済
                exit_price = current_price
                pnl_usdt = contracts * (exit_price - entry_price) * LEVERAGE * (1 if side == 'long' else -1) 
                pnl_rate = (exit_price / entry_price - 1) * LEVERAGE * (1 if side == 'long' else -1)
                
                trade_result = {
                    'status': 'closed',
                    'exit_type': exit_type,
                    'exit_price': exit_price,
                    'entry_price': entry_price,
                    'pnl_usdt': pnl_usdt,
                    'pnl_rate': pnl_rate,
                    'filled_amount': contracts,
                }
                
                telegram_message = format_telegram_message(pos, "ポジション決済 (TEST)", get_current_threshold(GLOBAL_MACRO_CONTEXT), trade_result, exit_type)
                await send_telegram_notification(telegram_message)
                
                log_signal(pos, "ポジション決済", trade_result)

                positions_to_remove.append(pos)

    # 監視ループを回りきった後に、決済されたポジションを削除
    OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
    
async def main_bot_scheduler():
    """
    メインの分析・取引ループを定期的に実行するスケジューラ
    """
    global LAST_SUCCESS_TIME, LAST_WEBSHARE_UPLOAD_TIME

    # 初回起動時にクライアントを初期化
    if not await initialize_exchange_client():
        await send_telegram_notification("❌ **BOT起動失敗**: CCXTクライアントの初期化に失敗しました。BOTを停止します。")
        sys.exit(1)

    while True:
        # 致命的なエラーが発生した場合のリカバリと遅延
        if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
             logging.error("❌ CCXTクライアントが切断されています。1分間待機して再初期化を試みます。")
             await asyncio.sleep(60)
             await initialize_exchange_client()
             continue

        current_time = time.time()
        
        try:
            await main_bot_loop()
            LAST_SUCCESS_TIME = time.time()
        except Exception as e:
            logging.critical(f"❌ メインループ実行中に致命的なエラー: {e}", exc_info=True)
            await send_telegram_notification(f"🚨 **致命的なエラー**\nメインループでエラーが発生しました: <code>{e}</code>")

        # WebShareログアップロードのチェック
        if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
            try:
                # ログファイルの内容を読み込み、WebShareのデータ構造に合わせて整形して送信
                webshare_data = {
                    'timestamp': datetime.now(JST).isoformat(),
                    'bot_version': BOT_VERSION,
                    'account_equity': ACCOUNT_EQUITY_USDT,
                    'open_positions_count': len(OPEN_POSITIONS),
                    'top_signals': [p for p in LAST_ANALYSIS_SIGNALS if p['score'] >= 0.70][:5], # 高スコアTop5
                    'fgi_context': GLOBAL_MACRO_CONTEXT,
                }
                await send_webshare_update(webshare_data)
                LAST_WEBSHARE_UPLOAD_TIME = current_time
            except Exception as e:
                logging.error(f"❌ WebShareログアップロード処理中にエラー: {e}")


        # 待機時間を LOOP_INTERVAL (60秒) に基づいて計算
        wait_time = max(1, LOOP_INTERVAL - (time.time() - LAST_SUCCESS_TIME))
        logging.info(f"(main_bot_scheduler) - 次のメインループまで {wait_time:.1f} 秒待機します。")
        await asyncio.sleep(wait_time)


async def position_monitor_scheduler():
    """
    ポジション監視ループを定期的に実行するスケジューラ
    """
    while True:
        if not EXCHANGE_CLIENT or not IS_CLIENT_READY:
            logging.debug("ポジション監視スケジューラ: クライアント未準備")
            await asyncio.sleep(MONITOR_INTERVAL)
            continue
            
        try:
            await position_monitor_loop()
        except Exception as e:
            logging.error(f"❌ ポジション監視ループ実行中にエラー: {e}", exc_info=True)
            
        logging.debug(f"(position_monitor_scheduler) - 次の監視ループまで {MONITOR_INTERVAL} 秒待機します。")
        await asyncio.sleep(MONITOR_INTERVAL)

# ====================================================================================
# FASTAPI APP
# ====================================================================================

app = FastAPI(title="Apex Trading Bot", version=BOT_VERSION)

@app.get("/health")
async def health_check():
    """ヘルスチェックエンドポイント"""
    return {"status": "ok", "version": BOT_VERSION}

@app.head("/")
async def head_check():
    """UptimeRobotなどのHEADメソッド対応 (軽量ヘルスチェック)"""
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
        logging.error(f"❌ 未処理の致命的なエラーが発生しました: {type(exc).__name__}: {exc}", exc_info=True)
    
    return JSONResponse(
        status_code=500,
        content={"message": f"Internal Server Error: {type(exc).__name__}"}
    )

if __name__ == "__main__":
    # 環境変数からポート番号を取得し、なければ8000を使用
    port = int(os.getenv("PORT", 8000)) 
    uvicorn.run(app, host="0.0.0.0", port=port)
