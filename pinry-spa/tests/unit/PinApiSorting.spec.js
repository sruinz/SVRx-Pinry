/* eslint-env jest */
import axios from 'axios';
import API from '@/components/api';

jest.mock('axios');

describe('Pin list sorting query', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    axios.get.mockResolvedValue({ data: { results: [], next: null } });
  });

  it('keeps legacy ordering without a sort state', async () => {
    await API.fetchPins(0, null, null, null);
    expect(axios.get.mock.calls[0][1].params.ordering).toBe('-id');
  });

  it('serializes random mode without legacy ordering', async () => {
    await API.fetchPins(30, 'tag', null, null, {
      version: 1, mode: 'random', randomSeed: 17,
    });
    expect(axios.get.mock.calls[0][1].params).toEqual({
      format: 'json',
      limit: 30,
      offset: 30,
      tags__name: 'tag',
      sort: 'random',
      random_seed: 17,
    });
  });

  it('serializes multiple tags as repeated exact query parameters', async () => {
    await API.fetchPins(0, ['alpha', 'β tag'], null, null);

    const config = axios.get.mock.calls[0][1];
    expect(config.params.tags__name).toEqual(['alpha', 'β tag']);
    expect(config.paramsSerializer(config.params)).toBe(
      'format=json&limit=30&offset=0&ordering=-id'
      + '&tags__name=alpha&tags__name=%CE%B2%20tag',
    );
  });
});
