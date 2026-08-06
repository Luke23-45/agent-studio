/**
 * Neryva Widget entry point.
 *
 * Registers the `<neryva-widget>` custom element and exports the class for
 * host pages that import the module directly.
 */

import { NeryvaWidget, WIDGET_TAG } from './ui/widget';

if (typeof customElements !== 'undefined' && !customElements.get(WIDGET_TAG)) {
  customElements.define(WIDGET_TAG, NeryvaWidget);
}

export { NeryvaWidget, WIDGET_TAG };
