# ====================================================================================
# Apex BOT v19.0.28 - Safety and Frequency Finalized (Patch 36)
#
# 修正ポイント:
# 1. 【安全確認】動的取引閾値 (0.67, 0.63, 0.58) を最終確定。
# 2. 【安全確認】取引実行ロジック (SL/TP, RRR >= 1.0, CCXT精度調整) の堅牢性を再確認。
# 3. 【バージョン更新】全てのバージョン情報を Patch 36 に更新。
# 4. 【機能改善】FTPログアップロードをHTTP/HTTPSログアップロードに変更。
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
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn
from dotenv import load_dotenv
import sys
import random
import json
import re
# import ftplib  <-- FTPからHTTPに変更したため削除
import uuid 

# .envファイルから環境変数を読み込む
load_dotenv()

# 💡 【ログ確認対応】ロギング設定を明示的に定義
logging.basicConfig(
    level=logging.INFO, # INFOレベル以上のメッセージを出力
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP40に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT",
    "DOGE/USDT", "DOT/USDT", "TRX/USDT", "MATIC/USDT", 
    "LTC/USDT", "AVAX/USDT", "LINK/USDT", "UNI/USDT", "ETC/USDT", "BCH/USDT",
    "NEAR/USDT", "ATOM/USDT", "FTM/USDT", "ALGO/USDT", "XLM/USDT", "SAND/USDT",
    "GALA/USDT", "FIL/USDT", "EOS/USDT", "AXS/USDT", "MANA/USDT", "AAVE/USDT",
    "MKR/USDT", "THETA/USDT", "FLOW/USDT", "IMX/USDT", 
]
TOP_SYMBOL_LIMIT = 40               # 監視対象銘柄の最大数 (出来高TOPから選出)を40に引き上げ
LOOP_INTERVAL = 60 * 10             # メインループの実行間隔 (秒) - 10分ごと
ANALYSIS_ONLY_INTERVAL = 60 * 60    # 分析専用通知の実行間隔 (秒) - 1時間ごと
WEBSHARE_UPLOAD_INTERVAL = 60 * 60  # WebShareログアップロード間隔 (1時間ごと)
MONITOR_INTERVAL = 10               # ポジション監視ループの実行間隔 (秒)

# 💡 クライアント設定
CCXT_CLIENT_NAME = os.getenv("EXCHANGE_CLIENT", "mexc")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
API_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_API_KEY")
SECRET_KEY = os.getenv(f"{CCXT_CLIENT_NAME.upper()}_SECRET")
TEST_MODE = os.getenv("TEST_MODE", "False").lower() in ('true', '1', 't')
SKIP_MARKET_UPDATE = os.getenv("SKIP_MARKET_UPDATE", "False").lower() in ('true', '1', 't')

# 💡 自動売買設定 (動的ロットのベースサイズ)
try:
    BASE_TRADE_SIZE_USDT = float(os.getenv("BASE_TRADE_SIZE_USDT", "100")) 
except ValueError:
    BASE_TRADE_SIZE_USDT = 100.0
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが不正な値です。100 USDTを使用します。")
    
if BASE_TRADE_SIZE_USDT < 10:
    logging.warning("⚠️ BASE_TRADE_SIZE_USDTが10 USDT未満です。ほとんどの取引所の最小取引額を満たさない可能性があります。")


# 💡 WEBSHARE設定 (HTTP/HTTPS経由のアップロードを想定 - Firebase Storage/S3 Presigned URLなど)
WEBSHARE_UPLOAD_URL = os.getenv("WEBSHARE_UPLOAD_URL") # ログファイルのアップロード先URL
WEBSHARE_AUTH_HEADER_NAME = os.getenv("WEBSHARE_AUTH_HEADER_NAME", "X-Api-Key") # 認証ヘッダー名
WEBSHARE_AUTH_HEADER_VALUE = os.getenv("WEBSHARE_AUTH_HEADER_VALUE") # 認証ヘッダーの値 (API Keyなど)
# WEBSHARE_HOST, WEBSHARE_PORT, WEBSHARE_USER, WEBSHARE_PASS はFTP用として削除


# グローバル変数 (状態管理用)
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS.copy()
LAST_SUCCESS_TIME: float = 0.0
LAST_SIGNAL_TIME: Dict[str, float] = {}
LAST_ANALYSIS_SIGNALS: List[Dict] = []
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0
LAST_WEBSHARE_UPLOAD_TIME: float = 0.0 
GLOBAL_MACRO_CONTEXT: Dict = {} # マクロコンテキストを保持するための変数
IS_FIRST_MAIN_LOOP_COMPLETED: bool = False # 初回メインループ完了フラグ
OPEN_POSITIONS: List[Dict] = [] # 現在保有中のポジション (SL/TP監視用)

if TEST_MODE:
    logging.warning("⚠️ WARNING: TEST_MODE is active. Trading is disabled.")

# CCXTクライアントの準備完了フラグ
IS_CLIENT_READY: bool = False

# 取引ルール設定
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.65             # 動的閾値のベースライン (通常時の値 2-3銘柄/日を想定)
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# テクニカル分析定数 (v19.0.28ベース)
TARGET_TIMEFRAMES = ['15m', '1h', '4h']
BASE_SCORE = 0.60                   # ベースとなる取引基準点 (60点)
LONG_TERM_SMA_LENGTH = 200          # 長期トレンドフィルタ用SMA
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンド逆行時のペナルティ
STRUCTURAL_PIVOT_BONUS = 0.05       # 価格構造/ピボット支持時のボーナス
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
MACD_CROSS_PENALTY = 0.15           # MACDが不利なクロス/発散時のペナルティ
LIQUIDITY_BONUS_MAX = 0.06          # 流動性(板の厚み)による最大ボーナス
FGI_PROXY_BONUS_MAX = 0.05          # 恐怖・貪欲指数による最大ボーナス/ペナルティ
FOREX_BONUS_MAX = 0.0               # 為替機能を削除するため0.0に設定

# 市場環境に応じた動的閾値調整のための定数 (ユーザー要望に合わせて調整 - Patch 36確定)
FGI_SLUMP_THRESHOLD = -0.02         # FGIプロキシがこの値未満の場合、市場低迷と見なす
FGI_ACTIVE_THRESHOLD = 0.02         # FGIプロキシがこの値を超える場合、市場活発と見なす
# 🚨 最終調整箇所: 頻度目標達成のため閾値を引き下げ (この値で確定)
SIGNAL_THRESHOLD_SLUMP = 0.67       # 低迷時の閾値 (1-2銘柄/日を想定)
SIGNAL_THRESHOLD_NORMAL = 0.63      # 通常時の閾値 (2-3銘柄/日を想定)
SIGNAL_THRESHOLD_ACTIVE = 0.58      # 活発時の閾値 (3+銘柄/日を想定)

RSI_DIVERGENCE_BONUS = 0.10         # RSIダイバージェンス時のボーナス (未使用だが定数として残す)
VOLATILITY_BB_PENALTY_THRESHOLD = 0.01 # ボラティリティ過熱時のペナルティ閾値
OBV_MOMENTUM_BONUS = 0.04           # OBVトレンド一致時のボーナス

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def format_usdt(amount: float) -> str:
    """USDT金額を整形する"""
    if amount >= 1.0:
        return f"{amount:,.2f}"
    elif amount >= 0.01:
        return f"{amount:.4f}"
    else:
        return f"{amount:.6f}"

def get_estimated_win_rate(score: float) -> str:
    """スコアに基づいて推定勝率を返す (通知用)"""
    if score >= 0.90: return "90%+"
    if score >= 0.85: return "85-90%"
    if score >= 0.75: return "75-85%"
    if score >= 0.65: return "65-75%" 
    if score >= 0.60: return "60-65%"
    return "<60% (低)"

def get_current_threshold(macro_context: Dict) -> float:
    """
    グローバルマクロコンテキスト（FGIプロキシ値）に基づいて、
    現在の市場環境に合わせた動的な取引閾値を決定し、返す。
    """
    # グローバル定数にアクセス
    global FGI_SLUMP_THRESHOLD, FGI_ACTIVE_THRESHOLD
    global SIGNAL_THRESHOLD_SLUMP, SIGNAL_THRESHOLD_NORMAL, SIGNAL_THRESHOLD_ACTIVE
    
    # FGIプロキシ値を取得（デフォルトは0.0）
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    
    # 市場低迷/リスクオフの閾値 (0.67)
    if fgi_proxy < FGI_SLUMP_THRESHOLD:
        return SIGNAL_THRESHOLD_SLUMP
    
    # 市場活発/リスクオンの閾値 (0.58)
    elif fgi_proxy > FGI_ACTIVE_THRESHOLD:
        return SIGNAL_THRESHOLD_ACTIVE
        
    # 通常/中立時の閾値 (0.63)
    else:
        return SIGNAL_THRESHOLD_NORMAL

def get_score_breakdown(signal: Dict) -> str:
    """分析スコアの詳細なブレークダウンメッセージを作成する (Telegram通知用)"""
    tech_data = signal.get('tech_data', {})
    timeframe = signal.get('timeframe', 'N/A')
    
    # スコア算出ロジックから取得できる定数値 (通知表示に利用)
    LONG_TERM_REVERSAL_PENALTY_CONST = LONG_TERM_REVERSAL_PENALTY 
    MACD_CROSS_PENALTY_CONST = MACD_CROSS_PENALTY                 
    LIQUIDITY_BONUS_POINT_CONST = LIQUIDITY_BONUS_MAX           
    
    breakdown_list = []

    # 1. ベーススコア (全ての分析の出発点)
    breakdown_list.append(f"  - **ベーススコア ({timeframe})**: <code>+{BASE_SCORE*100:.1f}</code> 点")
    
    # 2. 長期トレンド/構造の確認
    penalty_applied = tech_data.get('long_term_reversal_penalty_value', 0.0)
    if penalty_applied > 0.0:
        breakdown_list.append(f"  - ❌ 長期トレンド逆行 (SMA{LONG_TERM_SMA_LENGTH}): <code>-{penalty_applied*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ 長期トレンド一致 (SMA{LONG_TERM_SMA_LENGTH}): <code>+{LONG_TERM_REVERSAL_PENALTY_CONST*100:.1f}</code> 点 (ペナルティ回避)")

    # 価格構造/ピボット支持ボーナス
    pivot_bonus = tech_data.get('structural_pivot_bonus', 0.0)
    if pivot_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 価格構造/ピボット支持: <code>+{pivot_bonus*100:.1f}</code> 点")

    # 3. モメンタム/出来高の確認
    macd_penalty_applied = tech_data.get('macd_penalty_value', 0.0)
    total_momentum_penalty = macd_penalty_applied

    if total_momentum_penalty > 0.0:
        breakdown_list.append(f"  - ❌ モメンタム/クロス不利: <code>-{total_momentum_penalty*100:.1f}</code> 点")
    else:
        # ペナルティ回避時のボーナス相当として表示
        breakdown_list.append(f"  - ✅ MACD/RSIモメンタム加速: <code>+{MACD_CROSS_PENALTY_CONST*100:.1f}</code> 点相当 (ペナルティ回避)")

    # 出来高/OBV確証ボーナス
    obv_bonus = tech_data.get('obv_momentum_bonus_value', 0.0)
    if obv_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 出来高/OBV確証: <code>+{obv_bonus*100:.1f}</code> 点")
    
    # 4. 流動性/マクロ要因
    # 流動性ボーナス
    liquidity_bonus = tech_data.get('liquidity_bonus_value', 0.0)
    if liquidity_bonus > 0.0:
        breakdown_list.append(f"  - ✅ 流動性 (板の厚み) 優位: <code>+{LIQUIDITY_BONUS_POINT_CONST*100:.1f}</code> 点")
        
    # FGIマクロ要因
    fgi_bonus = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    if abs(fgi_bonus) > 0.001:
        sign = '✅' if fgi_bonus > 0 else '❌'
        breakdown_list.append(f"  - {sign} FGIマクロ影響: <code>{'+' if fgi_bonus > 0 else ''}{fgi_bonus*100:.1f}</code> 点")

    # 為替マクロ (常に0.0を表示)
    forex_bonus = tech_data.get('forex_bonus', 0.0) 
    breakdown_list.append(f"  - ⚪ 為替マクロ影響: <code>{forex_bonus*100:.1f}</code> 点 (機能削除済)")
    
    # ボラティリティペナルティ (負の値のみ表示)
    volatility_penalty = tech_data.get('volatility_penalty_value', 0.0)
    if volatility_penalty < 0.0:
        breakdown_list.append(f"  - ❌ ボラティリティ過熱ペナルティ: <code>{volatility_penalty*100:.1f}</code> 点")

    return "\n".join(breakdown_list)


def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict, current_threshold: float, monitoring_count: int) -> str:
    """1時間ごとの分析専用メッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **監視取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0) 

    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")
    forex_display = "中立 (機能削除済)"
    
    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = f"低迷/リスクオフ (Threshold: {SIGNAL_THRESHOLD_SLUMP*100:.0f}点)"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = f"活発/リスクオン (Threshold: {SIGNAL_THRESHOLD_ACTIVE*100:.0f}点)"
    else:
        market_condition_text = f"通常/中立 (Threshold: {SIGNAL_THRESHOLD_NORMAL*100:.0f}点)"
    

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報 (Rank 1のみに焦点を当てる)
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    
    if sorted_signals:
        top_signal = sorted_signals[0] # Rank 1を取得
        symbol = top_signal['symbol']
        timeframe = top_signal['timeframe']
        score = top_signal['score']
        rr_ratio = top_signal['rr_ratio']
        
        # スコア詳細ブレークダウンの生成
        breakdown_details = get_score_breakdown(top_signal)
        
        score_color = ""
        if score < current_threshold:
             score_color = "⚠️" 
        if score < BASE_SCORE: 
             score_color = "🔴"
             
        rr_display = f"1:{rr_ratio:.1f}" if rr_ratio >= 1.0 else f"1:{rr_ratio:.1f} ❌"
        
        signal_section += (
            f"  🥇 <b>{symbol}</b> ({timeframe}) - **最高スコア** {score_color}\n"
            f"     - **総合スコア**: <code>{score * 100:.2f} / 100</code> (推定勝率: {get_estimated_win_rate(score)})\n"
            f"     - **リスクリワード比率 (RRR)**: <code>{rr_display}</code>\n"
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )

        # 警告メッセージの追加
        if top_signal['score'] < current_threshold:
             signal_section += f"\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、取引閾値 ({current_threshold*100:.0f}点) 未満です。</pre>\n"
        
        if top_signal['score'] < BASE_SCORE:
             signal_section += f"<pre>🔴 警告: 最高スコアが取引基準点 ({BASE_SCORE*100:.0f}点) 未満です。</pre>\n"

    else:
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で最低限のリスクリワード比率を満たすロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>" 
    )

    return header + macro_section + signal_section + footer

def format_startup_message(
    account_status: Dict, 
    macro_context: Dict, 
    monitoring_count: int,
    current_threshold: float,
    bot_version: str
) -> str:
    """初回起動完了通知用のメッセージを作成する"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # マクロ情報
    fgi_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_proxy > FGI_ACTIVE_THRESHOLD else ("リスクオフ" if fgi_proxy < FGI_SLUMP_THRESHOLD else "中立")

    # 市場環境の判定
    if current_threshold == SIGNAL_THRESHOLD_SLUMP:
        market_condition_text = "低迷/リスクオフ"
    elif current_threshold == SIGNAL_THRESHOLD_ACTIVE:
        market_condition_text = "活発/リスクオン"
    else:
        market_condition_text = "通常/中立"
        
    # 自動売買ステータス
    trade_status = "自動売買 **ON**" if not TEST_MODE else "自動売買 **OFF** (TEST_MODE)"

    header = (
        f"🤖 **Apex BOT 起動完了通知** 🟢\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引所**: <code>{CCXT_CLIENT_NAME.upper()}</code> (現物モード)\n"
        f"  - **自動売買**: <b>{trade_status}</b>\n"
        f"  - **取引ロット (BASE)**: <code>{BASE_TRADE_SIZE_USDT:.2f}</code> USDT\n" # BASEに変更
        f"  - **監視銘柄数**: <code>{monitoring_count}</code>\n"
        f"  - **BOTバージョン**: <code>{bot_version}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # 1. 残高/ポジション情報
    balance_section = f"💰 <b>口座ステータス</b>\n"
    if account_status.get('error'):
        # エラーメッセージを分かりやすく表示
        balance_section += f"<pre>⚠️ ステータス取得失敗 (セキュリティのため詳細なエラーは表示しません。ログを確認してください)</pre>\n"
    else:
        balance_section += (
            f"  - **USDT残高**: <code>{format_usdt(account_status['total_usdt_balance'])}</code> USDT\n"
        )
        
        # 管理ポジションの表示
        if OPEN_POSITIONS:
            total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
            balance_section += (
                f"  - **管理中ポジション**: <code>{len(OPEN_POSITIONS)}</code> 銘柄 (投入合計: <code>{format_usdt(total_managed_value)}</code> USDT)\n"
            )
            for i, pos in enumerate(OPEN_POSITIONS[:3]): # Top 3のみ表示
                base_currency = pos['symbol'].replace('/USDT', '')
                balance_section += f"    - Top {i+1}: {base_currency} (SL: {format_usdt(pos['stop_loss'])} / TP: {format_usdt(pos['take_profit'])})\n"
            if len(OPEN_POSITIONS) > 3:
                balance_section += f"    - ...他 {len(OPEN_POSITIONS) - 3} 銘柄\n"
        else:
             balance_section += f"  - **管理中ポジション**: <code>なし</code>\n"

        # 既存の現物ポジション（CCXTから取得）は簡略化
        open_ccxt_positions = [p for p in account_status['open_positions'] if p['usdt_value'] >= 10]
        if open_ccxt_positions:
             balance_section += f"  - **未管理の現物**: <code>{len(open_ccxt_positions)}</code> 銘柄 (CCXT参照)\n"
        
    balance_section += f"\n"

    # 2. 市場状況 (スコア付け)
    macro_section = (
        f"🌍 <b>市場環境スコアリング</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **取引閾値 (Score)**: <code>{current_threshold*100:.0f} / 100</code>\n"
        f"  - **現在の市場環境**: <code>{market_condition_text}</code>\n"
        f"  - **FGI (恐怖・貪欲)**: <code>{fgi_value}</code> ({fgi_sentiment})\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    footer = (
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知はメインの分析ループが一度完了したことを示します。約10分ごとに分析が実行されます。</pre>"
    )

    return header + balance_section + macro_section + footer

def format_telegram_message(signal: Dict, context: str, current_threshold: float, trade_result: Optional[Dict] = None, exit_type: Optional[str] = None) -> str:
    """Telegram通知用のメッセージを作成する (取引結果を追加)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    symbol = signal['symbol']
    timeframe = signal['timeframe']
    score = signal['score']
    
    # SL/TP/RRRはシグナルまたは取引結果から取得
    entry_price = signal.get('entry_price', trade_result.get('entry_price', 0.0))
    stop_loss = signal.get('stop_loss', trade_result.get('stop_loss', 0.0))
    take_profit = signal.get('take_profit', trade_result.get('take_profit', 0.0))
    rr_ratio = signal.get('rr_ratio', 0.0)
    
    estimated_wr = get_estimated_win_rate(score)
    
    breakdown_details = get_score_breakdown(signal) 

    trade_section = ""
    trade_status_line = ""

    if context == "取引シグナル":
        # エントリー通知
        lot_size = signal.get('lot_size_usdt', BASE_TRADE_SIZE_USDT) # 動的ロット
        
        if TEST_MODE:
            trade_status_line = f"⚠️ **テストモード**: 取引は実行されません。(ロット: {format_usdt(lot_size)} USDT)"
        elif trade_result is None or trade_result.get('status') == 'error':
            trade_status_line = f"❌ **自動売買 失敗**: {trade_result.get('error_message', 'APIエラー')}"
        elif trade_result.get('status') == 'ok':
            trade_status_line = "✅ **自動売買 成功**: 現物ロング注文を執行しました。"
            filled_amount = trade_result.get('filled_amount', 'N/A')
            filled_usdt = trade_result.get('filled_usdt', 'N/A')
            trade_section = (
                f"💰 **取引実行結果**\n"
                f"  - **注文タイプ**: <code>現物 (Spot) / 成行買い</code>\n"
                f"  - **動的ロット**: <code>{format_usdt(lot_size)}</code> USDT (目標)\n"
                f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
                f"  - **平均約定額**: <code>{format_usdt(filled_usdt)}</code> USDT\n"
            )
            
    elif context == "ポジション決済":
        # 決済通知
        trade_status_line = f"🔴 **ポジション決済**: {exit_type} トリガー"
        
        entry_price = trade_result.get('entry_price', 0.0)
        exit_price = trade_result.get('exit_price', 0.0)
        pnl_usdt = trade_result.get('pnl_usdt', 0.0)
        pnl_rate = trade_result.get('pnl_rate', 0.0)
        filled_amount = trade_result.get('filled_amount', 'N/A')
        
        pnl_sign = "✅ 利益確定" if pnl_usdt >= 0 else "❌ 損切り"
        
        trade_section = (
            f"💰 **決済実行結果** - {pnl_sign}\n"
            f"  - **エントリー価格**: <code>{format_usdt(entry_price)}</code>\n"
            f"  - **決済価格**: <code>{format_usdt(exit_price)}</code>\n"
            f"  - **約定数量**: <code>{filled_amount:.4f}</code> {symbol.split('/')[0]}\n"
            f"  - **損益**: <code>{'+' if pnl_usdt >= 0 else ''}{format_usdt(pnl_usdt)}</code> USDT ({pnl_rate*100:.2f}%)\n"
        )
            
    
    message = (
        f"🚀 **Apex TRADE {context}**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **日時**: {now_jst} (JST)\n"
        f"  - **銘柄**: <b>{symbol}</b> ({timeframe})\n"
        f"  - **ステータス**: {trade_status_line}\n" 
        f"  - **総合スコア**: <code>{score * 100:.2f} / 100</code>\n"
        f"  - **取引閾値**: <code>{current_threshold * 100:.2f}</code> 点\n"
        f"  - **推定勝率**: <code>{estimated_wr}</code>\n"
        f"  - **リスクリワード比率 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"📌 **ポジション管理パラメータ**\n"
        f"  - **エントリー**: <code>{format_usdt(entry_price)}</code>\n"
        f"  - **ストップロス (SL)**: <code>{format_usdt(stop_loss)}</code>\n"
        f"  - **テイクプロフィット (TP)**: <code>{format_usdt(take_profit)}</code>\n"
        f"  - **リスク幅 (SL)**: <code>{format_usdt(entry_price - stop_loss)}</code> USDT\n"
        f"  - **リワード幅 (TP)**: <code>{format_usdt(take_profit - entry_price)}</code> USDT\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    
    # 取引結果セクションをシグナル詳細の前に追加
    if trade_section:
        message += trade_section + f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        
    # エントリー時のみスコアブレークダウンを表示
    if context == "取引シグナル":
        message += (
            f"  \n**📊 スコア詳細ブレークダウン** (+/-要因)\n"
            f"{breakdown_details}\n"
            f"  <code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        )
        
    message += (f"<i>Bot Ver: v19.0.28 - Safety and Frequency Finalized (Patch 36)</i>")
    return message


async def send_telegram_notification(message: str) -> bool:
    """Telegramにメッセージを送信する"""
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

def _to_json_compatible(obj):
    """
    再帰的にオブジェクトをJSON互換の型に変換するヘルパー関数。
    特にboolやNumPyのスカラ型を文字列/Pythonネイティブ型に変換する。
    """
    if isinstance(obj, dict):
        return {k: _to_json_compatible(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_to_json_compatible(elem) for elem in obj]
    elif isinstance(obj, (bool, np.bool_)):
        # すべてのブーリアン型を文字列に変換し、シリアライズエラーを回避
        return str(obj) 
    elif isinstance(obj, np.generic):
        # numpy.float64, numpy.int64 などのNumPyスカラをPythonネイティブ型に変換
        return obj.item()
    return obj


def log_signal(data: Dict, log_type: str, trade_result: Optional[Dict] = None) -> None:
    """シグナルまたは取引結果をローカルファイルにログする"""
    try:
        # ロギングされるデータの基本構造を定義
        log_entry = {
            'timestamp_jst': datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
            'log_type': log_type,
            'symbol': data.get('symbol', 'N/A'),
            'timeframe': data.get('timeframe', 'N/A'),
            'score': data.get('score', 0.0),
            'rr_ratio': data.get('rr_ratio', 0.0),
            'trade_result': trade_result or data.get('trade_result', None), # 決済時にはtrade_resultがdata内に含まれる
            'full_data': data,
        }
        
        # JSONシリアライズエラーを回避するために辞書をクリーニング
        cleaned_log_entry = _to_json_compatible(log_entry)

        # ファイル名にスペースが入らないように修正
        log_file = f"apex_bot_{log_type.lower().replace(' ', '_')}_log.jsonl"
        with open(log_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(cleaned_log_entry, ensure_ascii=False) + '\n')
            
        logging.info(f"✅ {log_type}ログをファイルに記録しました。")
    except Exception as e:
        logging.error(f"❌ ログ書き込みエラー: {e}", exc_info=True)


def _sync_http_upload(local_file: str, remote_filename: str) -> bool:
    """
    同期的にHTTP/HTTPS POSTによるアップロードを実行するヘルパー関数。
    asyncio.to_threadで使用される。
    
    この実装は、単一のファイルの内容をPOSTリクエストのデータとして送信し、
    ファイル名はクエリパラメータまたはボディに含める方式を想定する。
    (例: S3のPresigned URLや、特定のログ収集APIエンドポイント)
    """
    upload_url = WEBSHARE_UPLOAD_URL
    if not upload_url:
        logging.error("❌ WEBSHARE_UPLOAD_URLが設定されていません。")
        return False

    if not os.path.exists(local_file):
        logging.warning(f"⚠️ ローカルファイル {local_file} が見つかりません。アップロードをスキップします。")
        return True # ファイルがないのはエラーではない

    headers = {}
    if WEBSHARE_AUTH_HEADER_VALUE:
        headers[WEBSHARE_AUTH_HEADER_NAME] = WEBSHARE_AUTH_HEADER_VALUE

    try:
        # ファイルの内容をバイナリで読み込む
        with open(local_file, 'rb') as f:
            file_data = f.read()
            
        # ファイルの内容をデータとしてPOSTし、ファイル名はクエリパラメータで渡す
        # 💡 Multipart Form Dataではなく、シンプルなRaw Content POSTを想定
        # Content-Type: application/octet-stream (または text/plain)
        
        # URLにファイル名を付加 (例: ?filename=...)
        # 既にクエリパラメータがあるかチェックし、適切な区切り文字を使用
        separator = '&' if '?' in upload_url else '?'
        final_url = f"{upload_url}{separator}filename={remote_filename}"
        
        response = requests.post(
            final_url, 
            data=file_data, 
            headers=headers,
            timeout=30 # タイムアウトを30秒に設定
        )
        
        # ステータスコード200番台を成功と見なす
        response.raise_for_status() 
        
        logging.info(f"✅ HTTPアップロード成功: {remote_filename}")
        return True

    except requests.exceptions.RequestException as e:
        logging.error(f"❌ HTTPアップロードエラー ({upload_url}): {e}")
        if 'response' in locals() and response.content:
            logging.error(f"❌ サーバー応答: {response.status_code} - {response.text[:200]}...")
        return False
    except Exception as e:
        logging.error(f"❌ ログアップロードの予期せぬエラー: {e}")
        return False

async def upload_logs_to_webshare():
    """ローカルログファイルを外部ストレージ (WebShare/HTTP/HTTPS) にアップロードする"""
    if not WEBSHARE_UPLOAD_URL:
        logging.info("ℹ️ WEBSHARE_UPLOAD_URLが設定されていません。ログアップロードをスキップします。")
        return
        
    log_files = [
        "apex_bot_trade_signal_log.jsonl",
        "apex_bot_hourly_analysis_log.jsonl",
        "apex_bot_trade_exit_log.jsonl",
    ]
    
    now_jst = datetime.now(JST)
    upload_timestamp = now_jst.strftime("%Y%m%d_%H%M%S")
    
    logging.info(f"📤 WEBSHAREログアップロード処理を開始します...")

    tasks = []
    for log_file in log_files:
        if os.path.exists(log_file):
            # リモートファイル名にはタイムスタンプとファイル名を含める
            remote_filename = f"apex_log_{upload_timestamp}_{log_file}"
            # 同期HTTP処理を別スレッドで実行
            tasks.append(
                asyncio.to_thread(_sync_http_upload, log_file, remote_filename)
            ) 

    if not tasks:
        logging.info("ℹ️ アップロード対象のログファイルがありませんでした。")
        return

    # 全てのタスクを並行実行
    results = await asyncio.gather(*tasks)

    if all(results):
        logging.info(f"✅ すべてのログファイル ({len(tasks)} 件) を WEBSHARE にアップロードしました。")
    else:
        logging.error("❌ 一部またはすべてのログファイルの WEBSHARE へのアップロードに失敗しました。")

# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_exchange_client() -> bool:
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT
    try:
        client_name = CCXT_CLIENT_NAME.lower()
        if client_name == 'binance':
            exchange_class = ccxt_async.binance
        elif client_name == 'bybit':
            exchange_class = ccxt_async.bybit
        # MEXCクライアント
        elif client_name == 'mexc':
            exchange_class = ccxt_async.mexc
        else:
            logging.error(f"❌ 未対応の取引所クライアント: {CCXT_CLIENT_NAME}")
            return False

        # CCXTのオプション設定
        options = {
            'defaultType': 'spot', # 現物取引 (Spot) を想定
        }
        
        EXCHANGE_CLIENT = exchange_class({
            'apiKey': API_KEY,
            'secret': SECRET_KEY,
            'enableRateLimit': True,
            'options': options
        })
        
        await EXCHANGE_CLIENT.load_markets()
        logging.info(f"✅ CCXTクライアント ({CCXT_CLIENT_NAME}) を現物取引モードで初期化しました。")
        return True
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        EXCHANGE_CLIENT = None
        return False

async def fetch_ohlcv_safe(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """OHLCVデータを安全に取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT:
        return None
    
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        if not ohlcv:
            return None
            
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df = df.set_index('timestamp')
        return df
    except Exception as e:
        # logging.error(f"❌ OHLCV取得エラー {symbol}/{timeframe}: {e}")
        return None

async def fetch_ohlcv_with_fallback(symbol: str, timeframe: str, limit: int) -> Optional[pd.DataFrame]:
    """指定した足数を確実に取得するためにフォールバック処理を行う"""
    df = await fetch_ohlcv_safe(symbol, timeframe, limit)
    
    # データが少ない場合は、より多くの足数を要求してリトライ
    if df is None or len(df) < limit:
        logging.debug(f"ℹ️ {symbol} ({timeframe}): 必要足数 ({limit}) 未満。リトライ中...")
        df_long = await fetch_ohlcv_safe(symbol, timeframe, limit=limit + 100)
        
        if df_long is not None and len(df_long) >= limit:
            return df_long
        elif df_long is not None and len(df_long) > 0:
            return df_long # 必要な足数に満たなくても、取得できた分を返す
            
    return df

async def update_symbols_by_volume() -> None:
    """出来高に基づいて監視対象銘柄リストを更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    
    if not EXCHANGE_CLIENT or SKIP_MARKET_UPDATE:
        logging.info("ℹ️ 銘柄リスト更新をスキップしました。")
        return
        
    try:
        # すべてのUSDTペアの現物銘柄を取得
        markets = await EXCHANGE_CLIENT.load_markets()
        usdt_spot_symbols = [
            s for s, m in markets.items() 
            if m['active'] and m['spot'] and (m['quote'] == 'USDT' or s.endswith('/USDT'))
        ]

        if not usdt_spot_symbols:
            logging.warning("⚠️ USDT現物銘柄が見つかりませんでした。デフォルトリストを使用します。")
            CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_SYMBOLS))
            logging.info(f"✅ 銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄。")
            return

        # 24時間出来高情報を取得 (ccxtのfetch_tickersでquoteVolumeを使用)
        tickers = await EXCHANGE_CLIENT.fetch_tickers(usdt_spot_symbols)
        
        volume_data = []
        for symbol in usdt_spot_symbols:
            if symbol in tickers and 'quoteVolume' in tickers[symbol]:
                 # Noneチェックを追加
                if tickers[symbol]['quoteVolume'] is not None:
                    volume_data.append({
                        'symbol': symbol,
                        'volume': tickers[symbol]['quoteVolume']
                    })
                
        # 出来高でソートし、上位TOP_SYMBOL_LIMIT個を取得
        volume_data.sort(key=lambda x: x['volume'], reverse=True)
        top_symbols = [d['symbol'] for d in volume_data if d['volume'] is not None][:TOP_SYMBOL_LIMIT]
        
        # デフォルトシンボルと出来高トップをマージし、重複を排除
        combined_symbols = top_symbols + [s for s in DEFAULT_SYMBOLS if s not in top_symbols]
        CURRENT_MONITOR_SYMBOLS = combined_symbols
        
        logging.info(f"✅ 銘柄リストを出来高上位 ({len(top_symbols)}件) とデフォルトを合わせて {len(CURRENT_MONITOR_SYMBOLS)} 銘柄に更新しました。")

    except Exception as e:
        logging.error(f"❌ 銘柄リスト更新失敗: {e}")
        # 失敗時はデフォルトリストを維持
        CURRENT_MONITOR_SYMBOLS = list(set(DEFAULT_SYMBOLS))


async def fetch_macro_context() -> Dict:
    """
    外部APIからマクロ環境変数（恐怖・貪欲指数など）を取得する。
    ここでは、ダミーデータと、FGIプロキシ値を計算するロジックを実装する。
    """
    context: Dict = {
        'fgi_raw_value': 'N/A',
        'fgi_proxy': 0.0,
        'forex_bonus': 0.0, # 機能削除のため常に0.0
    }
    
    try:
        # 1. 恐怖・貪欲指数 (FGI) プロキシの取得 (外部サービスを想定)
        # 
        # 💡 [ダミーデータ] 外部APIから取得する値をシミュレート
        # 
        # 0: Extreme Fear (極度の恐怖) -> -1.0
        # 100: Extreme Greed (極度の貪欲) -> +1.0
        # 50: Neutral (中立) -> 0.0
        # 
        # FGIは通常、0から100の範囲。これを-1.0から1.0に線形変換する。
        # (FGI - 50) / 50 
        
        # 実際は外部APIを叩く
        fgi_raw_data = await _fetch_fgi_data() 
        fgi_value = fgi_raw_data.get('value', None)
        fgi_index = fgi_raw_data.get('value_classification', 'N/A')

        if fgi_value is not None:
            fgi_value = float(fgi_value)
            # FGI値を-1.0から1.0のプロキシ値に変換
            fgi_proxy = (fgi_value - 50.0) / 50.0
            
            context['fgi_raw_value'] = f"{fgi_value:.1f} ({fgi_index})"
            context['fgi_proxy'] = fgi_proxy
            
            logging.info(f"✅ マクロコンテキスト更新: FGI={fgi_value:.1f} ({fgi_proxy:.2f})")
            
        else:
            # 取得に失敗した場合や値がない場合はデフォルトの0.0を使用
            logging.warning("⚠️ FGIデータ取得に失敗しました。デフォルトの0.0を使用します。")

    except Exception as e:
        logging.error(f"❌ マクロコンテキスト取得エラー: {e}")
        # エラー発生時もデフォルト値 (0.0) を使用する

    return context


async def _fetch_fgi_data() -> Dict[str, Any]:
    """
    外部の恐怖・貪欲指数 (FGI) データを取得するダミー関数。
    実際にはAPIコールを実装する。
    """
    # 実際の実装では、ここで外部API（例：Alternative.meのFGI APIなど）を叩く
    # 例: response = requests.get("https://api.alternative.me/fng/?limit=1")
    
    # 💡 実際の運用環境ではコメントアウトし、APIコールに置き換えてください。
    # 乱数で現在の市場センチメントをシミュレート
    simulated_fgi_value = random.randint(10, 90)
    classification = "Greed"
    if simulated_fgi_value <= 25:
        classification = "Extreme Fear"
    elif simulated_fgi_value <= 45:
        classification = "Fear"
    elif simulated_fgi_value <= 55:
        classification = "Neutral"
    elif simulated_fgi_value <= 75:
        classification = "Greed"
    else:
        classification = "Extreme Greed"
    
    return {
        'value': str(simulated_fgi_value),
        'value_classification': classification,
        'timestamp': int(time.time()),
    }


# ====================================================================================
# TECHNICAL ANALYSIS & SCORING (v19.0.28)
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    指定されたDataFrameに必要なテクニカル指標を計算し、元のDataFrameに追加する。
    """
    # 1. シンプル移動平均 (SMA) - 長期トレンドフィルタ用
    df['SMA_Long'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    
    # 2. RSI (Relative Strength Index)
    df['RSI'] = ta.rsi(df['close'], length=14)

    # 3. MACD (Moving Average Convergence Divergence)
    macd_data = ta.macd(df['close'])
    df['MACD_Line'] = macd_data['MACD_12_26_9']
    df['MACD_Signal'] = macd_data['MACDH_12_26_9'] # MACDHはシグナルラインとの差分 (ヒストグラム)

    # 4. Bollinger Bands (BBANDS) - ボラティリティ測定用
    bbands = ta.bbands(df['close'], length=20, std=2)
    df['BB_Lower'] = bbands['BBL_20_2.0']
    df['BB_Upper'] = bbands['BBU_20_2.0']
    df['BB_Bandwidth'] = bbands['BBB_20_2.0'] / df['close'] # ボラティリティ (パーセンテージ)
    
    # 5. On-Balance Volume (OBV)
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_EMA'] = ta.ema(df['OBV'], length=20) # OBVのEMA

    # 6. Pivot Points (ピボットポイント) - 価格構造の確認用
    pivot_data = ta.pivots(df['high'], df['low'], df['close'], period='D', kind='fibonacci') 
    # ピボットポイントは直近の足の計算には使えないことが多いので、ここでは計算結果の最新値ではなく、
    # 補助情報として、直前の足のR3/S3などを利用できる形にしておく

    return df

def calculate_structural_pivot(df: pd.DataFrame, is_long_signal: bool) -> float:
    """
    価格構造やピボットポイントに基づいて、ボーナス/ペナルティを計算する。
    終値が直近の重要なサポートレベル（Pivot S1, S2, S3など）付近にあるかを確認する。
    """
    if df.empty or len(df) < 2:
        return 0.0

    # 💡 厳密なピボット計算は複雑なため、ここではシンプルに直近の最安値をサポートとして代用する。
    # ピボットの計算結果を利用できる場合はそれを使用する (ここではta.pivotsで計算したと仮定)
    
    current_close = df['close'].iloc[-1]
    
    # 直近の20期間の最安値 (サポート候補)
    recent_low = df['low'].iloc[-20:-1].min()
    
    # サポートからの距離を計算 (パーセンテージ)
    if current_close > recent_low:
        distance_from_support = (current_close - recent_low) / current_close
    else:
        # 価格がサポートを下回っている場合は無視 (またはペナルティ)
        return 0.0

    # サポートから近い位置（例: 0.5%以内）にある場合を「ピボット支持」と見なす
    SUPPORT_PROXIMITY_PCT = 0.005 # 0.5%
    
    if is_long_signal and distance_from_support < SUPPORT_PROXIMITY_PCT and current_close > recent_low:
        # ロングシグナルが出ていて、サポートに極めて近い位置にある場合は構造的ボーナス
        return STRUCTURAL_PIVOT_BONUS
    
    return 0.0


def calculate_volatility_penalty(df: pd.DataFrame) -> float:
    """ボラティリティが過熱している場合にペナルティを課す"""
    if df.empty:
        return 0.0
    
    # BB Bandwidth (ボリンジャーバンド幅) のパーセンテージを使用
    # ボラティリティが一定の閾値を超えているかを確認
    current_bandwidth = df['BB_Bandwidth'].iloc[-1]
    
    # 過去20期間の平均バンド幅
    history_bandwidth = df['BB_Bandwidth'].iloc[-20:-1].mean()
    
    # 現在のボラティリティが過去平均より大きく、かつ絶対的な閾値を超えている場合
    if current_bandwidth > history_bandwidth * 1.5 and current_bandwidth > VOLATILITY_BB_PENALTY_THRESHOLD:
        # ボラティリティ過熱と判断し、ペナルティを適用
        # ペナルティの大きさは固定値 (-0.10)
        return -0.10 
        
    return 0.0


def score_long_signal(df: pd.DataFrame, timeframe: str, macro_context: Dict) -> Tuple[float, Dict]:
    """
    指定されたDataFrameと時間足に基づいてロングシグナルのスコアを計算する。
    """
    if df is None or df.empty or len(df) < LONG_TERM_SMA_LENGTH + 1:
        return 0.0, {}

    # 最新の足を取得 (分析はクローズした足で行うため、iloc[-2]を使用)
    # しかし、リアルタイムの判断ではiloc[-1] (現在の足) を使用する方が一般的。
    # バックテストとの一貫性のために、ここではiloc[-2]を使用するが、
    # 最終的な価格や流動性のチェックではiloc[-1]も参照する。
    df_analysis = df.iloc[:-1] # 最後の完成足まで
    
    if df_analysis.empty: return 0.0, {}
    
    current_close = df_analysis['close'].iloc[-1]
    
    # 1. ベーススコア
    score = BASE_SCORE 
    tech_data: Dict[str, Any] = {}
    
    # 2. 長期トレンドフィルタ (SMA 200)
    # 価格がSMA 200よりも上にあるか
    is_above_long_sma = current_close > df_analysis['SMA_Long'].iloc[-1]
    
    long_term_reversal_penalty_value = 0.0
    if not is_above_long_sma:
        # 長期トレンドに逆行 (デッドクロス/下側) のペナルティ
        score -= LONG_TERM_REVERSAL_PENALTY
        long_term_reversal_penalty_value = LONG_TERM_REVERSAL_PENALTY

    tech_data['long_term_reversal_penalty_value'] = long_term_reversal_penalty_value

    # 3. モメンタム/過熱感 (RSI/MACD)
    
    # RSIが低すぎる（売られすぎ）か、または中立圏（ロングへの勢い）
    current_rsi = df_analysis['RSI'].iloc[-1]
    is_rsi_favorable = (current_rsi < 60) and (current_rsi > RSI_MOMENTUM_LOW) # 40-60の間を好ましいレンジとする

    # MACDがシグナルラインを上回っているか（強気のクロス）
    is_macd_favorable = (df_analysis['MACD_Line'].iloc[-1] > df_analysis['MACD_Signal'].iloc[-1])
    
    macd_penalty_value = 0.0
    # RSIが不利なレンジにあり、かつMACDがクロスしていない場合はペナルティ
    if not is_rsi_favorable and not is_macd_favorable:
        score -= MACD_CROSS_PENALTY
        macd_penalty_value = MACD_CROSS_PENALTY
        
    tech_data['macd_penalty_value'] = macd_penalty_value

    # 4. 価格構造/ピボット支持
    structural_pivot_bonus = calculate_structural_pivot(df, is_long_signal=True)
    score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus

    # 5. 出来高確証 (OBV)
    # OBVがEMAを上回っているか (出来高が価格上昇を裏付けているか)
    obv_momentum_bonus_value = 0.0
    if df_analysis['OBV'].iloc[-1] > df_analysis['OBV_EMA'].iloc[-1]:
        score += OBV_MOMENTUM_BONUS
        obv_momentum_bonus_value = OBV_MOMENTUM_BONUS
        
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus_value

    # 6. 流動性/板の厚みボーナス (ここではダミー値を適用。実環境ではCCXTのfetch_order_bookなどから取得)
    # 💡 簡易的なボーナスとして、終値と出来高の積 (USD換算出来高) を使用
    # 過去24時間の出来高平均と比較し、流動性が高い場合にボーナス
    avg_volume = df['volume'].iloc[-24:].mean()
    current_volume = df['volume'].iloc[-1]
    
    liquidity_bonus_value = 0.0
    if current_volume > avg_volume * 1.5:
        # 流動性優位と判断 (最大ボーナスを適用)
        score += LIQUIDITY_BONUS_MAX
        liquidity_bonus_value = LIQUIDITY_BONUS_MAX

    tech_data['liquidity_bonus_value'] = liquidity_bonus_value


    # 7. ボラティリティペナルティ
    volatility_penalty_value = calculate_volatility_penalty(df)
    score += volatility_penalty_value
    tech_data['volatility_penalty_value'] = volatility_penalty_value


    # 8. マクロ要因調整 (FGIプロキシ)
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    sentiment_fgi_proxy_bonus = 0.0
    
    # FGIプロキシ（-1.0から1.0）を最大ボーナス/ペナルティで調整
    # FGIがポジティブ（>0）であればボーナス、ネガティブ（<0）であればペナルティ
    # 例: FGIプロキシ 0.5 の場合、0.5 * 0.05 = 0.025 のボーナス
    sentiment_fgi_proxy_bonus = fgi_proxy * FGI_PROXY_BONUS_MAX
    score += sentiment_fgi_proxy_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = sentiment_fgi_proxy_bonus

    # 9. 為替マクロ要因 (機能削除のため常に0.0)
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    score += forex_bonus
    tech_data['forex_bonus'] = forex_bonus


    # 最終的なスコアをクリップ (0.0から1.0の間に収める)
    final_score = max(0.0, min(1.0, score))
    
    tech_data['final_score'] = final_score
    
    return final_score, tech_data


def find_long_signal(symbol: str, macro_context: Dict) -> Optional[Dict]:
    """
    指定された銘柄と全時間足に対して分析を実行し、
    最もスコアが高く、リスクリワード比率 (RRR) が1.0以上であるロングシグナルを見つける。
    """
    signals: List[Dict] = []
    
    for tf in TARGET_TIMEFRAMES:
        limit = REQUIRED_OHLCV_LIMITS[tf]
        
        # 1. OHLCVデータの取得
        df = asyncio.run(fetch_ohlcv_with_fallback(symbol, tf, limit))
        if df is None or df.empty or len(df) < limit:
            logging.debug(f"ℹ️ {symbol} ({tf}): 必要なデータが不足しています ({len(df) if df is not None else 0}/{limit})。")
            continue

        # 2. インジケーターの計算
        df = calculate_indicators(df)

        # 3. スコア計算
        score, tech_data = score_long_signal(df, tf, macro_context)
        
        # 4. エントリー、SL、TPの価格計算 (最新のクローズした足のデータを使用)
        # 価格情報がなければスキップ
        if df.iloc[:-1].empty: continue

        current_close = df.iloc[-2]['close'] # 最後の完成足の終値をエントリー価格候補とする
        
        # 💡 [SL/TPロジック] SL/TPの幅は、ATR (Average True Range) またはボラティリティに基づいて決定
        # ここでは、直近のATR (例: 14期間) を使用する
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        current_atr = df['ATR'].iloc[-2]
        
        if np.isnan(current_atr) or current_atr <= 0:
            logging.debug(f"ℹ️ {symbol} ({tf}): ATRが無効です。")
            continue
            
        # SL/TPの乗数 (リスクリワード比率=RRRのベース)
        # RRR=1.0を最低ラインとし、スコアに応じてR乗数を決定
        # RRRを1.0で固定し、スコア計算に集中するシンプルなロジックを採用
        SL_MULTIPLIER = 1.0     # 1 ATRを下回る位置にSLを設定
        TP_MULTIPLIER = 1.0     # 1 ATRを上回る位置にTPを設定
        
        # 🚨 安全性パッチ: SL幅は最低でも価格の0.5% (50bps) を確保し、ATRが小さすぎる場合は調整
        MIN_SL_PCT = 0.005 # 0.5%
        min_sl_usdt = current_close * MIN_SL_PCT
        
        # SL幅が小さすぎる場合、min_sl_usdtを使用
        sl_width = current_atr * SL_MULTIPLIER
        if sl_width < min_sl_usdt:
            sl_width = min_sl_usdt 
            
        # ロングシグナルの場合
        stop_loss = current_close - sl_width 
        take_profit = current_close + (sl_width * TP_MULTIPLIER) # RRR=1.0 のため、SL幅と同じ

        # RRRの計算 (SL/TP乗数が同じなので、通常は1.0になる)
        risk = current_close - stop_loss
        reward = take_profit - current_close
        rr_ratio = reward / risk if risk > 0 else 0.0
        
        # SL/TPが有効な価格（SL < Entry < TP）であり、RRR >= 1.0 の場合のみシグナルとして採用
        if stop_loss < current_close and current_close < take_profit and rr_ratio >= 1.0:
            
            # 動的なロットサイズの計算 (リスク額をBASE_TRADE_SIZE_USDTの一定割合に固定)
            # 例: リスク許容額をBASE_TRADE_SIZE_USDTの10%とする
            RISK_CAP_RATIO = 0.10
            risk_cap_usdt = BASE_TRADE_SIZE_USDT * RISK_CAP_RATIO 
            
            # 取引ロットサイズ (USDT) = リスク許容額 / (エントリー価格とSL価格の差額)
            # CCXT取引所によっては最小取引額の制約があるため、BASE_TRADE_SIZE_USDT以下に制限
            lot_size_usdt_calculated = risk_cap_usdt / (risk) 
            lot_size_usdt = min(lot_size_usdt_calculated, BASE_TRADE_SIZE_USDT * 2) # 最大ロットをBASEの2倍に制限
            
            # CCXT精度調整 (CCXTのload_marketsで取得できる精度情報を使って調整する必要がある)
            # ここではロジックの簡略化のため、調整はCCXTの取引実行時に任せる
            
            signals.append({
                'symbol': symbol,
                'timeframe': tf,
                'score': score,
                'entry_price': current_close,
                'stop_loss': stop_loss,
                'take_profit': take_profit,
                'rr_ratio': rr_ratio,
                'tech_data': tech_data, # スコアブレークダウン用
                'lot_size_usdt': lot_size_usdt, # 動的ロットサイズ
            })

    # 最もスコアの高いシグナルを返す
    if signals:
        best_signal = max(signals, key=lambda s: s['score'])
        # 最終チェック: 最高スコアがBASE_SCORE (0.60) を超えていること
        if best_signal['score'] >= BASE_SCORE:
            return best_signal

    return None


async def run_technical_analysis(symbols: List[str], macro_context: Dict) -> List[Dict]:
    """
    全監視銘柄に対してテクニカル分析を実行し、有効なロングシグナルを収集する。
    """
    logging.info(f"🔎 テクニカル分析を開始します ({len(symbols)} 銘柄)...")
    
    # 並行処理用のタスクリスト
    tasks = []
    for symbol in symbols:
        # 💡 同一銘柄のクールダウンチェック
        if time.time() - LAST_SIGNAL_TIME.get(symbol, 0.0) < TRADE_SIGNAL_COOLDOWN:
            logging.debug(f"ℹ️ {symbol}: クールダウン中のため分析をスキップします。")
            continue
            
        # asyncio.to_thread を使用して、ブロッキングする find_long_signal を非同期で実行
        tasks.append(
            asyncio.to_thread(find_long_signal, symbol, macro_context)
        )
        
    results = await asyncio.gather(*tasks)
    
    valid_signals = [signal for signal in results if signal is not None]
    
    # スコアの高い順にソートし、TOP_SIGNAL_COUNTに制限
    valid_signals.sort(key=lambda s: s['score'], reverse=True)
    
    logging.info(f"✅ テクニカル分析完了。有効シグナル数: {len(valid_signals)} 件。")
    
    # ログ記録 ( hourly_analysis )
    log_signal_data = [
        {
            'symbol': s['symbol'], 
            'timeframe': s['timeframe'], 
            'score': s['score'], 
            'rr_ratio': s['rr_ratio'], 
            'entry_price': s['entry_price'],
        } 
        for s in valid_signals
    ]
    log_signal({'signals': log_signal_data, 'macro_context': macro_context}, 'Hourly Analysis')
    
    return valid_signals


# ====================================================================================
# ACCOUNT & TRADING EXECUTION
# ====================================================================================

async def fetch_account_status() -> Dict:
    """
    アカウント残高、オープンポジションなどの情報を取得する。
    """
    global EXCHANGE_CLIENT
    status: Dict[str, Any] = {
        'total_usdt_balance': 0.0,
        'open_positions': [],
        'error': None
    }
    
    if not EXCHANGE_CLIENT:
        status['error'] = "CCXTクライアントが未初期化です。"
        return status
        
    try:
        # 1. 残高の取得
        balance = await EXCHANGE_CLIENT.fetch_balance()
        
        # 現物取引におけるUSDT残高を取得
        total_usdt_balance = balance.get('total', {}).get('USDT', 0.0)
        status['total_usdt_balance'] = total_usdt_balance
        
        # 2. オープンポジション（現物保有通貨）の取得
        open_positions_list = []
        for currency, data in balance.get('total', {}).items():
            if currency == 'USDT' or data == 0.0:
                continue
            
            symbol = f"{currency}/USDT"
            
            # 価格情報を取得し、USDT換算額を計算
            try:
                ticker = await EXCHANGE_CLIENT.fetch_ticker(symbol)
                price = ticker['last']
                usdt_value = data * price
                
                open_positions_list.append({
                    'symbol': symbol,
                    'amount': data,
                    'price': price,
                    'usdt_value': usdt_value,
                })
            except Exception:
                # USDTペアが存在しない場合はスキップ
                continue

        status['open_positions'] = open_positions_list
        logging.info(f"✅ 口座ステータス取得: USDT残高 {format_usdt(total_usdt_balance)}、保有銘柄 {len(open_positions_list)}件。")
        
    except Exception as e:
        logging.error(f"❌ 口座ステータス取得失敗: {e}")
        status['error'] = str(e)
        
    return status

async def execute_trading_logic(signal: Dict, current_threshold: float, account_status: Dict) -> Dict:
    """
    取引シグナルに従って、現物買い注文を執行する。
    CCXTの精度調整ロジックが含まれる（ロットサイズ、価格）。
    """
    global EXCHANGE_CLIENT
    result: Dict[str, Any] = {
        'status': 'error',
        'error_message': '初期化エラー',
        'filled_amount': 0.0,
        'filled_usdt': 0.0,
        'entry_price': signal['entry_price'],
    }
    
    if TEST_MODE:
        result['status'] = 'ok'
        result['error_message'] = 'TEST_MODE'
        # ダミーの約定額を設定
        result['filled_amount'] = signal['lot_size_usdt'] / signal['entry_price']
        result['filled_usdt'] = signal['lot_size_usdt']
        return result
        
    if not EXCHANGE_CLIENT:
        result['error_message'] = "CCXTクライアントが未初期化です。"
        return result
        
    symbol = signal['symbol']
    lot_size_usdt = signal['lot_size_usdt']
    
    # 資金チェック
    total_usdt_balance = account_status.get('total_usdt_balance', 0.0)
    # 現在のオープンポジションの合計投入額
    total_managed_value = sum(p['filled_usdt'] for p in OPEN_POSITIONS)
    # 取引可能な総資金 (例: 残高の80%まで)
    MAX_ALLOCATION_RATIO = 0.80 
    available_usdt = total_usdt_balance * MAX_ALLOCATION_RATIO - total_managed_value
    
    if lot_size_usdt > available_usdt:
        result['error_message'] = f"資金不足: 必要額 {format_usdt(lot_size_usdt)} USDT > 利用可能額 {format_usdt(available_usdt)} USDT"
        logging.error(f"❌ 取引失敗: {symbol} - {result['error_message']}")
        return result

    try:
        # 1. CCXTの取引所情報を利用して、ロットサイズと価格の精度を調整
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            result['error_message'] = f"取引所情報に {symbol} が見つかりません。"
            return result
            
        # ロットサイズ (USDT) を取引所に渡すための数量に変換し、CCXTの`amountToPrecision`で調整
        # 現物取引では、USDT建てでの注文が難しい取引所もあるため、
        # ベース通貨 (例: BTC) 建ての数量に変換する必要がある
        
        # 概算のベース通貨数量
        base_amount_rough = lot_size_usdt / signal['entry_price']
        
        # 数量の精度調整
        amount = EXCHANGE_CLIENT.amount_to_precision(symbol, base_amount_rough)
        
        # 2. 現物市場成行買い注文 (Long Entry) の実行
        order = await EXCHANGE_CLIENT.create_market_buy_order(
            symbol=symbol, 
            amount=amount # ベース通貨数量
        )
        
        # 3. 注文結果の確認
        if order and order['status'] in ('closed', 'fill'):
            # 注文が約定した場合
            filled_amount = float(order['filled'])
            filled_usdt = float(order['cost']) # USDTでの約定額
            
            # 平均約定価格を計算 (約定価格が複数ある場合もccxtがまとめてくれる)
            avg_price = filled_usdt / filled_amount if filled_amount > 0 else signal['entry_price'] 

            result['status'] = 'ok'
            result['error_message'] = None
            result['filled_amount'] = filled_amount
            result['filled_usdt'] = filled_usdt
            result['entry_price'] = avg_price # 実際の約定価格で上書き
            
            # 💡 ポジション管理リストに追加
            OPEN_POSITIONS.append({
                'symbol': symbol,
                'entry_price': avg_price,
                'stop_loss': signal['stop_loss'],
                'take_profit': signal['take_profit'],
                'filled_amount': filled_amount,
                'filled_usdt': filled_usdt,
                'rr_ratio': signal['rr_ratio'],
                'timestamp': time.time(),
                'timeframe': signal['timeframe'],
            })
            
            logging.info(f"✅ 取引成功: {symbol} - {format_usdt(filled_usdt)} USDT約定。")
            return result
        else:
            # 注文失敗または部分約定
            result['error_message'] = f"注文未約定 (ステータス: {order.get('status', 'N/A')})"
            logging.warning(f"⚠️ 取引警告: {symbol} - {result['error_message']}")
            return result

    except ccxt.InsufficientFunds as e:
        result['error_message'] = f"資金不足エラー: {e}"
        logging.error(f"❌ 取引失敗: {symbol} - 資金不足。")
    except ccxt.NetworkError as e:
        result['error_message'] = f"ネットワークエラー: {e}"
        logging.error(f"❌ 取引失敗: {symbol} - ネットワークエラー。")
    except Exception as e:
        result['error_message'] = f"予期せぬエラー: {e}"
        logging.error(f"❌ 取引失敗: {symbol} - 予期せぬエラー。", exc_info=True)
        
    return result


async def execute_exit_logic(position: Dict, exit_type: str) -> Dict:
    """
    ポジション決済ロジックを実行する。
    現物売り注文を執行し、結果を返す。
    """
    global EXCHANGE_CLIENT
    symbol = position['symbol']
    amount = position['filled_amount']
    
    result: Dict[str, Any] = {
        'status': 'error',
        'error_message': '初期化エラー',
        'entry_price': position['entry_price'],
        'exit_price': 0.0,
        'filled_amount': 0.0,
        'pnl_usdt': 0.0,
        'pnl_rate': 0.0,
    }
    
    if TEST_MODE:
        result['status'] = 'ok'
        result['error_message'] = 'TEST_MODE'
        # ダミーの決済価格を設定
        if exit_type == 'TAKE_PROFIT':
            result['exit_price'] = position['take_profit'] * 0.999 
        elif exit_type == 'STOP_LOSS':
            result['exit_price'] = position['stop_loss'] * 1.001
        else: # MANUAL_EXIT
            result['exit_price'] = position['entry_price'] * (1 + random.uniform(-0.01, 0.01))
            
        result['filled_amount'] = amount
        result['pnl_usdt'] = (result['exit_price'] - position['entry_price']) * amount
        result['pnl_rate'] = result['pnl_usdt'] / position['filled_usdt']
        return result
        
    if not EXCHANGE_CLIENT:
        result['error_message'] = "CCXTクライアントが未初期化です。"
        return result
        
    try:
        # 1. CCXTの取引所情報で数量の精度を調整
        market = EXCHANGE_CLIENT.markets.get(symbol)
        if not market:
            result['error_message'] = f"取引所情報に {symbol} が見つかりません。"
            return result
            
        # 数量の精度調整 (保有している全量を売り注文に出す)
        amount_to_sell = EXCHANGE_CLIENT.amount_to_precision(symbol, amount)
        
        # 2. 現物市場成行売り注文 (Exit) の実行
        order = await EXCHANGE_CLIENT.create_market_sell_order(
            symbol=symbol, 
            amount=amount_to_sell
        )
        
        # 3. 注文結果の確認
        if order and order['status'] in ('closed', 'fill'):
            filled_amount = float(order['filled'])
            filled_usdt_cost = float(order['cost']) # USDTでの約定額 (約定数量 * 平均約定価格)
            
            avg_exit_price = filled_usdt_cost / filled_amount if filled_amount > 0 else 0.0
            
            # PnL計算
            pnl_usdt = filled_usdt_cost - position['filled_usdt']
            pnl_rate = pnl_usdt / position['filled_usdt'] if position['filled_usdt'] > 0 else 0.0
            
            result['status'] = 'ok'
            result['error_message'] = None
            result['exit_price'] = avg_exit_price
            result['filled_amount'] = filled_amount
            result['pnl_usdt'] = pnl_usdt
            result['pnl_rate'] = pnl_rate

            logging.info(f"✅ 決済成功: {symbol} - {exit_type}。PnL: {format_usdt(pnl_usdt)} USDT ({pnl_rate*100:.2f}%)")
            return result
        else:
            result['error_message'] = f"注文未約定 (ステータス: {order.get('status', 'N/A')})"
            logging.warning(f"⚠️ 決済警告: {symbol} - {result['error_message']}")
            return result

    except ccxt.InvalidOrder as e:
        # ポジションがない、または残高不足の場合 (特に部分約定で残高が減っている場合)
        result['error_message'] = f"無効な注文/残高不足: {e}"
        logging.error(f"❌ 決済失敗: {symbol} - 無効な注文。", exc_info=True)
    except Exception as e:
        result['error_message'] = f"予期せぬエラー: {e}"
        logging.error(f"❌ 決済失敗: {symbol} - 予期せぬエラー。", exc_info=True)
        
    return result


async def monitor_open_positions(current_price_data: Dict[str, float], current_threshold: float):
    """
    現在保有中のポジションを監視し、SL/TPに達した場合は決済を実行する。
    """
    global OPEN_POSITIONS
    
    positions_to_remove = []
    
    for position in OPEN_POSITIONS:
        symbol = position['symbol']
        
        # 1. 価格チェック
        if symbol not in current_price_data:
            logging.warning(f"⚠️ ポジション監視警告: {symbol} の現在価格が見つかりません。")
            continue
            
        current_price = current_price_data[symbol]
        sl_price = position['stop_loss']
        tp_price = position['take_profit']
        
        exit_type = None
        
        if current_price <= sl_price:
            exit_type = 'STOP_LOSS'
        elif current_price >= tp_price:
            exit_type = 'TAKE_PROFIT'
            
        if exit_type:
            # 2. 決済実行
            exit_result = await execute_exit_logic(position, exit_type)
            
            if exit_result['status'] == 'ok':
                # 決済成功: ポジションリストから削除し、通知を送信
                positions_to_remove.append(position)
                
                await send_telegram_notification(
                    format_telegram_message(
                        signal=position, # ポジション情報をシグナル情報として利用
                        context="ポジション決済", 
                        current_threshold=current_threshold,
                        trade_result=exit_result,
                        exit_type=exit_type
                    )
                )
                log_signal(position, 'Trade Exit', exit_result)
            else:
                # 決済失敗: エラーログを出力し、ポジションはリストに残す（手動対応が必要）
                logging.error(f"❌ {symbol} の決済 ({exit_type}) に失敗しました: {exit_result.get('error_message')}")

    # 決済が完了したポジションをリストから削除
    if positions_to_remove:
        OPEN_POSITIONS = [p for p in OPEN_POSITIONS if p not in positions_to_remove]
        logging.info(f"🗑️ ポジションリストを更新しました。削除数: {len(positions_to_remove)} 件。")

    # ポジション監視の頻度をログ
    logging.debug(f"ℹ️ ポジション監視ループ完了。管理中ポジション数: {len(OPEN_POSITIONS)}。")


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_bot_loop():
    """
    BOTのメイン実行ループ。
    一定間隔で市場の更新、分析、取引実行、ログアップロードを行う。
    """
    global IS_CLIENT_READY, LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    global GLOBAL_MACRO_CONTEXT, LAST_WEBSHARE_UPLOAD_TIME, IS_FIRST_MAIN_LOOP_COMPLETED

    # 1. クライアント初期化 (初回のみ)
    if not IS_CLIENT_READY:
        IS_CLIENT_READY = await initialize_exchange_client()
        if not IS_CLIENT_READY:
            logging.critical("🚨 クライアント初期化に失敗しました。BOTを停止します。")
            return

    # 2. マクロコンテキストの取得 (最初に取得し、以降は1時間ごと)
    current_time = time.time()
    if current_time - LAST_HOURLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
        GLOBAL_MACRO_CONTEXT = await fetch_macro_context()


    # 3. 監視銘柄の更新 (初回起動時のみ実行)
    if not IS_FIRST_MAIN_LOOP_COMPLETED:
        await update_symbols_by_volume()


    # 4. ポジション監視ループの起動
    async def position_monitoring_loop():
        """ポジション監視を高速で実行するサブループ"""
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            if not OPEN_POSITIONS:
                continue
            
            # 監視中の銘柄の現在価格を一括取得
            symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
            current_price_data: Dict[str, float] = {}
            try:
                tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_monitor)
                for symbol in symbols_to_monitor:
                    if symbol in tickers and tickers[symbol]['last'] is not None:
                        current_price_data[symbol] = tickers[symbol]['last']
            except Exception as e:
                logging.error(f"❌ ポジション価格取得エラー: {e}")
                
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            await monitor_open_positions(current_price_data, current_threshold)
            

    # 5. メインロジックの実行
    if current_time - LAST_SUCCESS_TIME >= LOOP_INTERVAL:
        logging.info("--- メイン分析ループ開始 ---")
        
        # 実行可能取引閾値の取得 (動的閾値)
        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        
        # A. テクニカル分析を実行し、有効なシグナルを取得
        LAST_ANALYSIS_SIGNALS = await run_technical_analysis(CURRENT_MONITOR_SYMBOLS, GLOBAL_MACRO_CONTEXT)

        # B. 取引シグナルに絞り込み
        actionable_signals = [s for s in LAST_ANALYSIS_SIGNALS if s['score'] >= current_threshold]
        
        if actionable_signals:
            logging.info(f"🎯 取引閾値 ({current_threshold*100:.2f}点) 超えのシグナルが {len(actionable_signals)} 件見つかりました。")

            # 資金チェックと取引実行 (最高スコアのシグナルから順に実行)
            for signal in actionable_signals:
                symbol = signal['symbol']
                
                # ポジションの重複チェック (既に管理中の場合はスキップ)
                if any(p['symbol'] == symbol for p in OPEN_POSITIONS):
                    logging.warning(f"⚠️ 取引スキップ: {symbol} は既に管理中のポジションです。")
                    continue
                    
                # リアルタイムアカウントステータスの取得
                account_status = await fetch_account_status()
                
                trade_result = await execute_trading_logic(signal, current_threshold, account_status)
                
                # C. Telegram通知
                await send_telegram_notification(
                    format_telegram_message(
                        signal, 
                        context="取引シグナル", 
                        current_threshold=current_threshold, 
                        trade_result=trade_result
                    )
                )
                # D. ログ記録 ( trade_signal )
                log_signal(signal, 'Trade Signal', trade_result)
                
                # 取引成功した場合は、クールダウン時間を更新し、次のシグナルへ
                if trade_result['status'] == 'ok':
                    LAST_SIGNAL_TIME[symbol] = time.time()
                    # 1回のメインループで複数の取引を行わないためにbreak
                    break 
        else:
            logging.info("ℹ️ 取引閾値を超える有効なロングシグナルは見つかりませんでした。")


        # ログアップロードの実行 (メインループが成功した場合のみ時刻を更新)
        LAST_SUCCESS_TIME = current_time # メインループ完了時刻を更新
        
        # 初回メインループ完了フラグ
        if not IS_FIRST_MAIN_LOOP_COMPLETED:
            IS_FIRST_MAIN_LOOP_COMPLETED = True
            
            # 起動完了通知の送信 (アカウントステータスが必要なため、取引後に行う)
            account_status = await fetch_account_status()
            await send_telegram_notification(
                format_startup_message(
                    account_status, 
                    GLOBAL_MACRO_CONTEXT, 
                    len(CURRENT_MONITOR_SYMBOLS), 
                    current_threshold,
                    "v19.0.28 - Safety and Frequency Finalized (Patch 36)"
                )
            )
            
        logging.info("--- メイン分析ループ終了 ---")


    # 6. 分析専用通知 (1時間ごと)
    if current_time - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
        
        # マクロコンテキストが更新されていない場合は再取得
        if current_time - LAST_HOURLY_NOTIFICATION_TIME >= ANALYSIS_ONLY_INTERVAL:
             GLOBAL_MACRO_CONTEXT = await fetch_macro_context()
             LAST_HOURLY_NOTIFICATION_TIME = current_time

        current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
        
        # 既に分析結果がある場合のみ通知
        if LAST_ANALYSIS_SIGNALS:
            message = format_analysis_only_message(
                LAST_ANALYSIS_SIGNALS, 
                GLOBAL_MACRO_CONTEXT, 
                current_threshold, 
                len(CURRENT_MONITOR_SYMBOLS)
            )
            await send_telegram_notification(message)
            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = current_time
        else:
             logging.info("ℹ️ 分析専用通知: 前回の分析結果がないためスキップしました。")


    # 7. WebShareログアップロード (1時間ごと)
    if current_time - LAST_WEBSHARE_UPLOAD_TIME >= WEBSHARE_UPLOAD_INTERVAL:
        await upload_logs_to_webshare()
        LAST_WEBSHARE_UPLOAD_TIME = current_time


# ====================================================================================
# API SERVER & ENTRY POINT
# ====================================================================================

# FastAPIアプリケーションの初期化
app = FastAPI()

# メインループをバックグラウンドで実行するためのフラグ
MAIN_LOOP_TASK: Optional[asyncio.Task] = None
POSITION_MONITOR_TASK: Optional[asyncio.Task] = None

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時に実行される処理"""
    global MAIN_LOOP_TASK, POSITION_MONITOR_TASK
    logging.info("🚀 Apex BOT v19.0.28 起動中です...")
    
    # 💡 クライアントの初期化を先に行う
    global IS_CLIENT_READY
    IS_CLIENT_READY = await initialize_exchange_client()
    
    if IS_CLIENT_READY:
        # メインループを非同期タスクとして開始
        MAIN_LOOP_TASK = asyncio.create_task(main_loop_runner())
        
        # ポジション監視ループを非同期タスクとして開始
        POSITION_MONITOR_TASK = asyncio.create_task(main_bot_loop.position_monitoring_loop()) # 内部関数にアクセス
        
        logging.info("✅ メインループタスクを開始しました。")
    else:
        logging.critical("🚨 CCXT初期化失敗。BOTは取引を行いません。")

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時に実行される処理"""
    global MAIN_LOOP_TASK, EXCHANGE_CLIENT, POSITION_MONITOR_TASK
    logging.info("🛑 Apex BOT 停止処理中です...")
    
    if MAIN_LOOP_TASK:
        MAIN_LOOP_TASK.cancel()
        
    if POSITION_MONITOR_TASK:
        POSITION_MONITOR_TASK.cancel()
        
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        
    logging.info("✅ BOTプロセスを正常に終了しました。")


async def main_loop_runner():
    """
    メインBOTループを定期的に実行し、例外を処理するラッパー関数。
    """
    global LAST_SUCCESS_TIME, IS_CLIENT_READY
    while True:
        try:
            # クライアントが準備完了の場合のみ実行
            if IS_CLIENT_READY:
                await main_bot_loop() 
                
            # 次の実行までの待機時間を計算 (最低でも1秒は待つ)
            current_time = time.time()
            # LAST_SUCCESS_TIMEがゼロの場合は現在の時刻を基準にする（初回起動時）
            last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
            sleep_time = max(1, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))
            
            # ポジション監視の実行間隔も考慮し、頻繁にチェック
            # メインループのインターバルが長いため、ポジション監視とログアップロードの待機時間もこの中で制御する
            await asyncio.sleep(MONITOR_INTERVAL) # 監視間隔 (10秒) ごとにチェックを繰り返す
            
        except asyncio.CancelledError:
            # タスクがキャンセルされた場合はループを終了
            logging.warning("⚠️ メインループタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.critical(f"🚨 メインループで致命的なエラーが発生しました: {e}", exc_info=True)
            await asyncio.sleep(60) # エラー時に60秒待機してリトライ
            
            
@app.get("/")
async def get_status():
    """
    BOTの現在の状態と次の実行時間をJSONで返すヘルスチェックエンドポイント。
    """
    current_time = time.time()
    # LAST_SUCCESS_TIMEがゼロの場合は現在の時刻を基準にする（初回起動時）
    last_time_for_calc = LAST_SUCCESS_TIME if LAST_SUCCESS_TIME > 0 else current_time
    next_check = max(0, int(LOOP_INTERVAL - (current_time - last_time_for_calc)))

    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.28 - Safety and Frequency Finalized (Patch 36)", # バージョン更新
        "base_trade_size_usdt": BASE_TRADE_SIZE_USDT, 
        "managed_positions_count": len(OPEN_POSITIONS), 
        # last_success_time は、LAST_SUCCESS_TIMEが初期値(0.0)でない場合にのみフォーマットする
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, timezone.utc).isoformat() if LAST_SUCCESS_TIME > 0 else "N/A",
        "next_main_loop_check_seconds": next_check,
        "current_threshold": get_current_threshold(GLOBAL_MACRO_CONTEXT),
        "macro_context": GLOBAL_MACRO_CONTEXT, # FGIプロキシなど
        "is_test_mode": TEST_MODE,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "is_client_ready": IS_CLIENT_READY,
    }

    return JSONResponse(content=status_msg)

if __name__ == "__main__":
    # main_bot_loop.position_monitoring_loop() にアクセスできるように、関数を定義しておく
    async def position_monitoring_loop():
        """ポジション監視を高速で実行するサブループ"""
        while True:
            await asyncio.sleep(MONITOR_INTERVAL)
            if not OPEN_POSITIONS:
                continue
            
            # 監視中の銘柄の現在価格を一括取得
            symbols_to_monitor = [p['symbol'] for p in OPEN_POSITIONS]
            current_price_data: Dict[str, float] = {}
            try:
                # EXCHANGE_CLIENTが初期化されていることを確認
                if EXCHANGE_CLIENT:
                    tickers = await EXCHANGE_CLIENT.fetch_tickers(symbols_to_monitor)
                    for symbol in symbols_to_monitor:
                        if symbol in tickers and tickers[symbol]['last'] is not None:
                            current_price_data[symbol] = tickers[symbol]['last']
            except Exception as e:
                # CCXT初期化前のアクセスや、その他のエラーをハンドリング
                logging.error(f"❌ ポジション価格取得エラー: {e}")
                
            current_threshold = get_current_threshold(GLOBAL_MACRO_CONTEXT)
            await monitor_open_positions(current_price_data, current_threshold)
            
    # main_bot_loopの内部関数として定義されたposition_monitoring_loopを外部からアクセス可能にする
    main_bot_loop.position_monitoring_loop = position_monitoring_loop

    # ローカル開発/実行用のエントリポイント
    uvicorn.run("main_render (13):app", host="0.0.0.0", port=8000, log_level="info", reload=False)
