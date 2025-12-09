#!/usr/bin/env python3
"""
Batch statistics for Majsoul game records
Aggregate statistics by player

Statistics:
- Riichi count, riichi rate
- Furo (call) rounds, furo rate
- Win count, win rate
- Deal-in count, deal-in rate
- Total win points, average win points
- Total deal-in points, average deal-in points

Usage:
    python stats_batch.py --csv <csv_file>
"""

import json
import csv
import re
import sys
import argparse
from pathlib import Path
from collections import defaultdict


def parse_player_name(player_str: str) -> tuple[str, str]:
    """
    Parse player string to extract account ID and nickname
    Format: [Server][AccountID]Nickname
    Returns: (account_id, nickname)
    """
    match = re.match(r'\[([^\]]+)\]\[(\d+)\](.+)', player_str)
    if match:
        return match.group(2), match.group(3)
    return "", player_str


def load_csv_records(csv_path: str) -> dict:
    """
    Load CSV game records
    Returns: {paipu_id: {players: [...], ...}}
    """
    records = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Try common column names
            paipu_id = row.get('牌谱链接', '') or row.get('paipu_id', '') or row.get('uuid', '')
            paipu_id = paipu_id.strip()
            if not paipu_id:
                continue
            
            players = []
            for i in range(1, 5):
                # Try both Chinese and English column names
                player_str = row.get(f'{i}位玩家', '') or row.get(f'player_{i}', '')
                score = row.get(f'{i}位分数', '') or row.get(f'score_{i}', '0')
                account_id, nickname = parse_player_name(player_str)
                players.append({
                    'rank': i,
                    'account_id': account_id,
                    'nickname': nickname,
                    'score': score
                })
            
            records[paipu_id] = {
                'start_time': row.get('开始时间', '') or row.get('start_time', ''),
                'end_time': row.get('结束时间', '') or row.get('end_time', ''),
                'players': players
            }
    
    return records


def analyze_paipu_json(json_path: str) -> dict:
    """
    Analyze a single JSON game record
    Returns detailed statistics
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    # Initialize statistics for each seat
    seat_stats = {i: {
        'riichi_count': 0,
        'furo_round_count': 0,
        'win_count': 0,
        'deal_in_count': 0,
        'win_points': 0,
        'deal_in_points': 0,
        'round_count': 0,
    } for i in range(4)}
    
    final_scores = None
    current_round_furo = {i: False for i in range(4)}
    
    actions = data.get('actions', [])
    
    for action in actions:
        action_type = action.get('type', '')
        action_data = action.get('data', {})
        
        # New round starts
        if action_type == 'RecordNewRound':
            # Count furo from previous round
            for i in range(4):
                if current_round_furo[i]:
                    seat_stats[i]['furo_round_count'] += 1
            
            # Reset current round state
            current_round_furo = {i: False for i in range(4)}
            # Increment round count
            for i in range(4):
                seat_stats[i]['round_count'] += 1
        
        # Count riichi
        elif action_type == 'RecordDiscardTile':
            if action_data.get('is_liqi'):
                seat = action_data.get('seat', 0)
                seat_stats[seat]['riichi_count'] += 1
        
        # Count furo (chi/pon/kan) - only mark if called this round
        elif action_type == 'RecordChiPengGang':
            seat = action_data.get('seat', 0)
            current_round_furo[seat] = True
        
        # Count wins
        elif action_type == 'RecordHule':
            hules = action_data.get('hules', [])
            for hule in hules:
                winner_seat = hule.get('seat', 0)
                is_zimo = hule.get('zimo', False)
                dadian = hule.get('dadian', 0)
                
                # Win statistics
                seat_stats[winner_seat]['win_count'] += 1
                seat_stats[winner_seat]['win_points'] += dadian
                
                # Deal-in statistics (non-tsumo)
                if not is_zimo:
                    delta_scores = action_data.get('delta_scores', [])
                    if delta_scores:
                        min_delta = min(delta_scores)
                        for i, delta in enumerate(delta_scores):
                            if delta == min_delta and i != winner_seat and delta < 0:
                                seat_stats[i]['deal_in_count'] += 1
                                seat_stats[i]['deal_in_points'] += dadian
                                break
        
        # Get final scores
        if action_type in ['RecordHule', 'RecordNoTile']:
            scores = action_data.get('scores')
            if scores:
                final_scores = scores
    
    # Count furo from last round
    for i in range(4):
        if current_round_furo[i]:
            seat_stats[i]['furo_round_count'] += 1
    
    return {
        'seat_stats': seat_stats,
        'final_scores': final_scores
    }


def match_players_by_score(csv_players: list, final_scores: list) -> dict:
    """
    Match CSV players to JSON seats by final score
    Returns: {seat: player_info}
    """
    if not final_scores or len(final_scores) != 4:
        return {}
    
    csv_scores = []
    for p in csv_players:
        try:
            score = int(float(p.get('score', '0')))
            csv_scores.append((p, score))
        except (ValueError, TypeError):
            csv_scores.append((p, 0))
    
    seat_to_player = {}
    used_players = set()
    
    for seat, json_score in enumerate(final_scores):
        best_match = None
        best_diff = float('inf')
        
        for i, (player, csv_score) in enumerate(csv_scores):
            if i in used_players:
                continue
            diff = abs(json_score - csv_score)
            if diff < best_diff:
                best_diff = diff
                best_match = (i, player)
        
        if best_match and best_diff < 100:
            used_players.add(best_match[0])
            seat_to_player[seat] = best_match[1]
    
    return seat_to_player


def batch_analyze(csv_path: str, output_dir: Path) -> dict:
    """
    Batch analyze all game records, aggregate by player
    """
    # Load CSV records
    csv_records = load_csv_records(csv_path)
    print(f"Loaded {len(csv_records)} records from CSV")
    
    # Player statistics
    player_stats = defaultdict(lambda: {
        'nickname': '',
        'account_id': '',
        'game_count': 0,
        'round_count': 0,
        'riichi_count': 0,
        'furo_round_count': 0,
        'win_count': 0,
        'deal_in_count': 0,
        'win_points': 0,
        'deal_in_points': 0,
    })
    
    # Process each game record
    processed = 0
    skipped = 0
    
    for paipu_id, csv_record in csv_records.items():
        json_path = output_dir / f'{paipu_id}.json'
        
        if not json_path.exists():
            skipped += 1
            continue
        
        try:
            # Analyze JSON
            analysis = analyze_paipu_json(str(json_path))
            seat_stats = analysis['seat_stats']
            final_scores = analysis['final_scores']
            
            # Match players
            csv_players = csv_record.get('players', [])
            seat_to_player = match_players_by_score(csv_players, final_scores)
            
            # Accumulate statistics
            for seat, player in seat_to_player.items():
                account_id = player.get('account_id', '')
                if not account_id:
                    continue
                
                stats = player_stats[account_id]
                stats['nickname'] = player.get('nickname', '')
                stats['account_id'] = account_id
                stats['game_count'] += 1
                
                seat_stat = seat_stats.get(seat, {})
                stats['round_count'] += seat_stat.get('round_count', 0)
                stats['riichi_count'] += seat_stat.get('riichi_count', 0)
                stats['furo_round_count'] += seat_stat.get('furo_round_count', 0)
                stats['win_count'] += seat_stat.get('win_count', 0)
                stats['deal_in_count'] += seat_stat.get('deal_in_count', 0)
                stats['win_points'] += seat_stat.get('win_points', 0)
                stats['deal_in_points'] += seat_stat.get('deal_in_points', 0)
            
            processed += 1
            
        except Exception as e:
            print(f"Error processing {paipu_id}: {e}")
            skipped += 1
    
    print(f"Processed: {processed}, Skipped: {skipped}")
    return dict(player_stats)


def calculate_rates(stats: dict) -> dict:
    """
    Calculate various rates
    """
    round_count = stats['round_count']
    if round_count == 0:
        round_count = 1  # Avoid division by zero
    
    win_count = stats['win_count']
    deal_in_count = stats['deal_in_count']
    
    return {
        **stats,
        'riichi_rate': stats['riichi_count'] / round_count * 100,
        'furo_rate': stats['furo_round_count'] / round_count * 100,
        'win_rate': win_count / round_count * 100,
        'deal_in_rate': deal_in_count / round_count * 100,
        'avg_win_points': stats['win_points'] / win_count if win_count > 0 else 0,
        'avg_deal_in_points': stats['deal_in_points'] / deal_in_count if deal_in_count > 0 else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Batch statistics for Majsoul game records")
    parser.add_argument("--csv", type=str, required=True, help="Path to CSV file")
    args = parser.parse_args()
    
    # Path configuration
    base_dir = Path(__file__).parent
    output_dir = base_dir / 'output'
    
    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV file not found {csv_path}")
        sys.exit(1)
    
    # Batch analyze
    print("Starting batch analysis...")
    print(f"CSV file: {csv_path}")
    print(f"Paipu directory: {output_dir}")
    print("-" * 50)
    
    player_stats = batch_analyze(str(csv_path), output_dir)
    
    # Calculate rates and sort
    players_with_rates = [calculate_rates(s) for s in player_stats.values()]
    sorted_players = sorted(players_with_rates, key=lambda x: x['game_count'], reverse=True)
    
    # Output results
    print("\n" + "=" * 130)
    print("Player Statistics")
    print("=" * 130)
    
    # Headers
    headers = [
        ('Player', 16), ('Games', 5), ('Rounds', 6),
        ('Riichi', 6), ('R.Rate', 6),
        ('Furo', 5), ('F.Rate', 6),
        ('Wins', 5), ('W.Rate', 6),
        ('Deal-in', 7), ('D.Rate', 6),
        ('WinPts', 8), ('AvgWin', 7),
        ('DealPts', 8), ('AvgDeal', 7),
    ]
    
    header_line = ''.join(f"{h[0]:<{h[1]}}" for h in headers)
    print(header_line)
    print("-" * 130)
    
    for p in sorted_players:
        line = (
            f"{p['nickname']:<16}"
            f"{p['game_count']:<5}"
            f"{p['round_count']:<6}"
            f"{p['riichi_count']:<6}"
            f"{p['riichi_rate']:<6.1f}"
            f"{p['furo_round_count']:<5}"
            f"{p['furo_rate']:<6.1f}"
            f"{p['win_count']:<5}"
            f"{p['win_rate']:<6.1f}"
            f"{p['deal_in_count']:<7}"
            f"{p['deal_in_rate']:<6.1f}"
            f"{p['win_points']:<8}"
            f"{p['avg_win_points']:<7.0f}"
            f"{p['deal_in_points']:<8}"
            f"{p['avg_deal_in_points']:<7.0f}"
        )
        print(line)
    
    # Save to CSV (Chinese headers)
    result_csv_path = base_dir / 'player_stats.csv'
    with open(result_csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            '玩家', '账号ID', '对局数', '总局数',
            '立直次数', '立直率',
            '副露局数', '副露率',
            '和牌次数', '和牌率',
            '放铳次数', '放铳率',
            '和牌总打点', '平均打点',
            '放铳总点数', '平均放铳打点'
        ])
        for p in sorted_players:
            writer.writerow([
                p['nickname'],
                p['account_id'],
                p['game_count'],
                p['round_count'],
                p['riichi_count'],
                f"{p['riichi_rate']:.2f}%",
                p['furo_round_count'],
                f"{p['furo_rate']:.2f}%",
                p['win_count'],
                f"{p['win_rate']:.2f}%",
                p['deal_in_count'],
                f"{p['deal_in_rate']:.2f}%",
                p['win_points'],
                f"{p['avg_win_points']:.0f}",
                p['deal_in_points'],
                f"{p['avg_deal_in_points']:.0f}",
            ])
    
    print(f"\nResults saved to: {result_csv_path}")


if __name__ == '__main__':
    main()
