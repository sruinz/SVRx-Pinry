const path = require('path');

module.exports = {
  process(source, filename) {
    return { code: `module.exports = ${JSON.stringify(path.basename(filename))};` };
  },
};
