import random


def distribute_fruits(baskets, fruit_varieties, existing_distribution=None):
    """Distributes fruit varieties across baskets with balanced counts.

    Args:
        baskets: The number of baskets (n).
        fruit_varieties: A dictionary where keys are fruit names and values are their quantities.
        existing_distribution: An optional list of dictionaries representing an existing distribution.

    Returns:
        A list of dictionaries, where each dictionary represents a basket and contains
        the distributed fruits and their quantities. Returns None if input is invalid.
    """

    if not isinstance(baskets, int) or baskets <= 0:
        print("Error: Number of baskets must be a positive integer.")
        return None
    if not isinstance(fruit_varieties, dict):  # Allow empty dict for adding to existing
        print("Error: Fruit varieties must be a dictionary.")
        return None
    for quantity in fruit_varieties.values():
        if not isinstance(quantity, int) or quantity < 0:
            print("Error: Fruit quantities must be non-negative integers.")
            return None

    if existing_distribution is None:
        distributed_baskets = [{} for _ in range(baskets)]
    else:
        if not isinstance(existing_distribution, list) or len(existing_distribution) != baskets:
            print("Error: Existing distribution must be a list with the same length as the number of baskets.")
            return None
        distributed_baskets = [basket.copy() for basket in
                               existing_distribution]  # Create a copy to avoid modifying the original

    total_new_fruits = sum(fruit_varieties.values())
    total_existing_fruits = sum(sum(basket.values()) for basket in distributed_baskets)
    total_fruits = total_new_fruits + total_existing_fruits

    if total_fruits < baskets:
        print("Warning: There are fewer fruits than baskets. Some baskets will be empty or have less variety.")

    # Calculate target number of fruits per basket
    target_per_basket = total_fruits // baskets
    remainder = total_fruits % baskets

    # Distribute the remainder first, one to each basket until it runs out.
    for i in range(remainder):
        distributed_baskets[i]["remainder_fruit"] = distributed_baskets[i].get("remainder_fruit", 0) + 1

    for fruit, quantity in fruit_varieties.items():
        if quantity == 0:
            continue
        for _ in range(quantity):
            # Find the basket with the fewest fruits
            min_basket_index = 0
            min_count = sum(distributed_baskets[0].values())
            for i in range(1, baskets):
                current_count = sum(distributed_baskets[i].values())
                if current_count < min_count:
                    min_count = current_count
                    min_basket_index = i

            distributed_baskets[min_basket_index][fruit] = distributed_baskets[min_basket_index].get(fruit, 0) + 1

    # Remove the remainder fruit key.
    for basket in distributed_baskets:
        if "remainder_fruit" in basket:
            del basket["remainder_fruit"]

    return distributed_baskets


# Example usage:
num_baskets = 3
initial_fruits = {
    "apples": 5,
    "bananas": 3
}

initial_distribution = distribute_fruits(num_baskets, initial_fruits)

if initial_distribution:
    print("Initial Distribution:")
    for i, basket in enumerate(initial_distribution):
        print(f"Basket {i + 1}: {basket} (Total: {sum(basket.values())})")

new_fruits = {
    "oranges": 4,
    "grapes": 2
}

new_distribution = distribute_fruits(num_baskets, new_fruits, initial_distribution)

if new_distribution:
    print("\nDistribution after adding new fruits:")
    for i, basket in enumerate(new_distribution):
        print(f"Basket {i + 1}: {basket} (Total: {sum(basket.values())})")

# Example with Empty new fruits dictionary.
new_fruits_empty = {}
new_distribution_empty = distribute_fruits(num_baskets, new_fruits_empty, initial_distribution)

if new_distribution_empty:
    print("\nDistribution after adding no new fruits:")
    for i, basket in enumerate(new_distribution_empty):
        print(f"Basket {i + 1}: {basket} (Total: {sum(basket.values())})")

# Example with invalid existing distribution
invalid_existing_distribution = [{}, {}]
new_distribution_invalid = distribute_fruits(num_baskets, new_fruits, invalid_existing_distribution)