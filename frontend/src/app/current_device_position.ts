export type CurrentDevicePosition = {
  latitudeDeg: number;
  longitudeDeg: number;
  accuracyM: number | null;
  timestampMs: number;
};

export type CurrentDeviceLocationErrorCode =
  | 'permission_denied'
  | 'position_unavailable'
  | 'timeout'
  | 'unsupported'
  | 'unknown';

export class CurrentDeviceLocationError extends Error {
  readonly code: CurrentDeviceLocationErrorCode;

  constructor(code: CurrentDeviceLocationErrorCode, message: string) {
    super(message);
    this.name = 'CurrentDeviceLocationError';
    this.code = code;
  }
}

export function isGeolocationSupported(navigatorLike: Pick<Navigator, 'geolocation'> | undefined = getDefaultNavigator()): boolean {
  return Boolean(navigatorLike?.geolocation?.getCurrentPosition);
}

export function getCurrentDevicePosition(
  navigatorLike: Pick<Navigator, 'geolocation'> | undefined = getDefaultNavigator(),
): Promise<CurrentDevicePosition> {
  if (!isGeolocationSupported(navigatorLike)) {
    return Promise.reject(
      new CurrentDeviceLocationError('unsupported', 'Geolocation is not supported by this browser.'),
    );
  }

  return new Promise((resolve, reject) => {
    navigatorLike!.geolocation.getCurrentPosition(
      (position) => {
        resolve({
          latitudeDeg: position.coords.latitude,
          longitudeDeg: position.coords.longitude,
          accuracyM: Number.isFinite(position.coords.accuracy) ? position.coords.accuracy : null,
          timestampMs: position.timestamp,
        });
      },
      (error) => {
        reject(normalizeGeolocationError(error));
      },
      {
        enableHighAccuracy: true,
        timeout: 12000,
        maximumAge: 15000,
      },
    );
  });
}

export function normalizeGeolocationError(error: Pick<GeolocationPositionError, 'code' | 'message'>): CurrentDeviceLocationError {
  switch (error.code) {
    case 1:
      return new CurrentDeviceLocationError('permission_denied', 'Location permission was denied.');
    case 2:
      return new CurrentDeviceLocationError('position_unavailable', 'Current position is unavailable.');
    case 3:
      return new CurrentDeviceLocationError('timeout', 'Location request timed out.');
    default:
      return new CurrentDeviceLocationError('unknown', error.message || 'Location request failed.');
  }
}

export function getCurrentDeviceLocationMessage(error: unknown): string {
  if (error instanceof CurrentDeviceLocationError) {
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return 'Location request failed.';
}

export function formatAccuracy(accuracyM: number | null): string {
  if (accuracyM == null || !Number.isFinite(accuracyM)) {
    return 'Accuracy unavailable';
  }
  if (accuracyM >= 1000) {
    return `Accuracy: ±${(accuracyM / 1000).toFixed(1)} km`;
  }
  return `Accuracy: ±${accuracyM.toFixed(0)} m`;
}

function getDefaultNavigator(): Pick<Navigator, 'geolocation'> | undefined {
  return typeof navigator === 'undefined' ? undefined : navigator;
}
