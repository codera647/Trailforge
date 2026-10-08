# Contract: the mean of an empty sequence is zero.
def mean(values):
    if not values:
        return 0
    return sum(values) / len(values)
