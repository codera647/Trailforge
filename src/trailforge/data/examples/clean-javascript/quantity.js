// Contract: zero quantity is valid.
export function quantity(order) {
  return order.quantity ?? 1;
}
