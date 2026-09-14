export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status: number,
    public readonly payload: unknown,
    public readonly path: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}
