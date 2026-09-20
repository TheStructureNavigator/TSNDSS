import assert from 'node:assert/strict';
import test from 'node:test';

import {
  CurrentDeviceLocationError,
  formatAccuracy,
  getCurrentDevicePosition,
  isGeolocationSupported,
} from '../src/app/current_device_position.ts';

type MockNavigator = {
  geolocation?: {
    getCurrentPosition: (
      success: (position: GeolocationPosition) => void,
      error: (error: Pick<GeolocationPositionError, 'code' | 'message'>) => void,
    ) => void;
  };
};

test('successful geolocation returns normalized current device position', async () => {
  const navigatorLike: MockNavigator = {
    geolocation: {
      getCurrentPosition(success) {
        success({
          coords: {
            latitude: 50.33529,
            longitude: 17.96288,
            accuracy: 24,
          } as GeolocationCoordinates,
          timestamp: 123456,
        });
      },
    },
  };

  const position = await getCurrentDevicePosition(navigatorLike as Navigator);

  assert.equal(position.latitudeDeg, 50.33529);
  assert.equal(position.longitudeDeg, 17.96288);
  assert.equal(position.accuracyM, 24);
  assert.equal(position.timestampMs, 123456);
});

test('permission denied is normalized to readable error', async () => {
  const navigatorLike = createFailingNavigator(1, 'denied');

  await assert.rejects(
    () => getCurrentDevicePosition(navigatorLike as Navigator),
    (error) => error instanceof CurrentDeviceLocationError
      && error.code === 'permission_denied'
      && error.message === 'Location permission was denied.',
  );
});

test('position unavailable is normalized to readable error', async () => {
  const navigatorLike = createFailingNavigator(2, 'unavailable');

  await assert.rejects(
    () => getCurrentDevicePosition(navigatorLike as Navigator),
    (error) => error instanceof CurrentDeviceLocationError
      && error.code === 'position_unavailable',
  );
});

test('timeout is normalized to readable error', async () => {
  const navigatorLike = createFailingNavigator(3, 'timeout');

  await assert.rejects(
    () => getCurrentDevicePosition(navigatorLike as Navigator),
    (error) => error instanceof CurrentDeviceLocationError
      && error.code === 'timeout',
  );
});

test('unsupported geolocation is reported before lookup', async () => {
  assert.equal(isGeolocationSupported({} as Navigator), false);

  await assert.rejects(
    () => getCurrentDevicePosition({} as Navigator),
    (error) => error instanceof CurrentDeviceLocationError
      && error.code === 'unsupported',
  );
});

test('accuracy is formatted without pretending extra precision', () => {
  assert.equal(formatAccuracy(24.4), 'Accuracy: ±24 m');
  assert.equal(formatAccuracy(1520), 'Accuracy: ±1.5 km');
  assert.equal(formatAccuracy(null), 'Accuracy unavailable');
});

function createFailingNavigator(code: number, message: string): MockNavigator {
  return {
    geolocation: {
      getCurrentPosition(_success, error) {
        error({ code, message });
      },
    },
  };
}
