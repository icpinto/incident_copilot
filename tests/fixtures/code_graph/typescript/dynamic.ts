export function invoke(target: Record<string, () => unknown>, key: string) {
  return target[key]();
}
