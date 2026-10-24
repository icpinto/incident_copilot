export function charge() {
  throw new Error("gateway timeout");
}

export function ping() {
  return true;
}
