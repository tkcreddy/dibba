def greedy_coin_change(amount, denominations):
    denominations.sort(reverse=True)  # Sort denominations in descending order
    coins_used = []

    for coin in denominations:
        while amount >= coin:
            coins_used.append(coin)
            amount -= coin

    return coins_used

# Example usage:
amount_to_change = 63
coin_denominations = [1, 2, 5, 10, 20]

result = greedy_coin_change(amount_to_change, coin_denominations)
print("Coins Used:", result)
print("Total Coins:", len(result))