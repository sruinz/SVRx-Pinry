/* eslint-env jest */

import modals, {
  openBoardDelete,
  openExport,
  openPinBulkBoard,
  openPinBulkEdit,
} from '@/components/modals';
import { overlayState } from '@/components/utils/overlays';

function lastWidth() {
  return overlayState.modals[overlayState.modals.length - 1].width;
}

describe('modal sizing', () => {
  afterEach(() => {
    [...overlayState.modals].reverse().forEach(entry => entry.close());
  });

  it('uses a compact width for account and board forms', () => {
    modals.openAdd2Board({}, { id: 1 }, 'owner');
    expect(lastWidth()).toBe('480px');
    modals.openBoardCreate({});
    expect(lastWidth()).toBe('480px');
    modals.openLogin({}, jest.fn());
    expect(lastWidth()).toBe('480px');
  });

  it('keeps Pin creation wider than Pin editing', () => {
    modals.openPinEdit({}, { username: 'owner' });
    expect(lastWidth()).toBe('900px');
    modals.openPinEdit({}, { username: 'owner', isEdit: true });
    expect(lastWidth()).toBe('760px');
  });

  it('uses a medium width for bulk, deletion, and export dialogs', () => {
    const props = { selectedIds: [1, 2] };
    openPinBulkBoard({}, { ...props, mode: 'add' });
    expect(lastWidth()).toBe('560px');
    openPinBulkEdit({}, props);
    expect(lastWidth()).toBe('560px');
    openBoardDelete({}, { board: { id: 3 } });
    expect(lastWidth()).toBe('560px');
    openExport({}, { pinIds: [1] });
    expect(lastWidth()).toBe('560px');
  });
});
