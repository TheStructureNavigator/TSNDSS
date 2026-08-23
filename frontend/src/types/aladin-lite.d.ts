declare module 'aladin-lite' {
  const A: {
    init: Promise<void>;
    aladin: (selector: string, options?: Record<string, unknown>) => unknown;
  };

  export default A;
}
