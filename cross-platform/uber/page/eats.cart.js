/**
 * page/eats.cart.js — cart / checkout verbs on window.__ueats.
 *
 * Loaded after page/eats.js (same inject). Owns draft resolve, add-to-cart,
 * preview confirmation, and place-order wire shapes. Python keeps AgentOS
 * shaping, delivery-address policy, PaymentDisplayMissing, and the human
 * gate before checkout().
 *
 * Bump __cartV to force-reload after edits.
 */
(function () {
  const U = globalThis.__ueats;
  if (!U || typeof U.api !== 'function') {
    throw new Error('page/eats.cart.js requires page/eats.js (__ueats) first');
  }
  if ((U.__cartV || 0) >= 1) return;
  U.__cartV = 1;

  const CHECKOUT_PAYLOAD_TYPES = [
    'canonicalProductStorePickerPayload', 'fulfillmentPromotionInfo',
    'deliveryOptInInfo', 'eta', 'fareBreakdown', 'upfrontTipping',
    'basketSizeTracker', 'total', 'cartItems', 'subtotal', 'promotion',
    'disclaimers', 'orderConfirmations', 'passBanner', 'taxProfiles',
    'addressNudge', 'basketSize', 'complements', 'messageBanner',
    'merchantMembership', 'giftInfo', 'restrictedItems',
    'timeWindowPicker', 'locationInfo', 'paymentProfilesEligibility',
    'upsellCatalogSections', 'subTotalFareBreakdown',
    'storeSwitcherActionableBannerPayload',
    'promoAndMembershipSavingBannerPayloadCheckout',
    'promoAndMembershipSavingBannerPayload', 'venueSectionPicker',
    'paymentBarPayload', 'neutralZonePayload', 'allDetailsHeader',
    'allDetailsActions', 'subsRenewalBanner',
    'splitPaymentMessageBanner', 'upsellFeed', 'requestUtensilPayload',
  ];

  const richText = (node) => {
    if (node == null) return null;
    if (typeof node === 'string') return node;
    if (Array.isArray(node)) {
      const parts = node.map(richText).filter(Boolean);
      return parts.length ? parts.join(' ') : null;
    }
    if (typeof node === 'object') {
      if (typeof node.text === 'string') return node.text;
      if (node.text && typeof node.text === 'object') {
        const nested = richText(node.text);
        if (nested) return nested;
      }
      if (Array.isArray(node.richTextElements)) {
        const parts = node.richTextElements.map(richText).filter(Boolean);
        return parts.length ? parts.join(' ') : null;
      }
      return richText(node.title) || richText(node.subtitle);
    }
    return null;
  };

  const amountE5 = (v) => {
    if (v == null) return null;
    if (typeof v === 'object') {
      const low = v.low || 0;
      const high = v.high || 0;
      if (high < 0) return -((~low & 0xffffffff) + 1) / 1e5;
      return low / 1e5;
    }
    const n = Number(v);
    return Number.isFinite(n) ? n / 1e5 : null;
  };

  const newUuid = () =>
    (typeof crypto !== 'undefined' && crypto.randomUUID
      ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
          const r = (Math.random() * 16) | 0;
          const v = c === 'x' ? r : (r & 0x3) | 0x8;
          return v.toString(16);
        }));

  U._richText = richText;
  U._amountE5 = amountE5;

  U.buildCustomizations = (customizations) => {
    if (!customizations) return {};
    if (customizations && typeof customizations === 'object' && !Array.isArray(customizations)) {
      return customizations;
    }
    const result = {};
    for (const group of customizations) {
      const groupUuid = group.group_uuid || group.uuid || '';
      const groupId =
        group.group_id != null ? group.group_id : group.groupId != null ? group.groupId : 0;
      const groupTitle = group.group_title || group.title || '';
      const key = `${groupUuid}+${groupId}`;
      const options = [];
      for (const sel of group.selections || []) {
        const opt = {
          uuid: sel.uuid || '',
          price: sel.price || 0,
          quantity: sel.quantity != null ? sel.quantity : 1,
          title: sel.title || '',
          defaultQuantity: sel.defaultQuantity || 0,
          customizationMeta: {
            title: groupTitle,
            isPickOne: !!group.isPickOne,
          },
        };
        if (sel.childCustomizations) {
          opt.childCustomizations = U.buildCustomizations(sel.childCustomizations);
        }
        options.push(opt);
      }
      result[key] = options;
    }
    return result;
  };

  /** Replay catalog _raw into a createDraftOrder cart line. */
  U.buildCartItem = (product, storeUuid) => {
    const raw = product && product._raw;
    if (!raw) {
      return {
        ok: false,
        error:
          "Cart item missing _raw — products must come from get_store (replay, don't reconstruct)",
      };
    }
    for (const required of ['uuid', 'sectionUuid', 'title', 'price', 'imageUrl']) {
      if (!raw[required]) {
        return {
          ok: false,
          error: `Catalog item missing required field '${required}'`,
          keys: Object.keys(raw),
        };
      }
    }
    const quantity = product.quantity != null ? product.quantity : 1;
    const purchase = ((raw.purchaseInfo || {}).purchaseOptions || [{}])[0] || {};
    const pricing = (raw.purchaseInfo || {}).pricingInfo || {};
    const item = {
      uuid: raw.uuid,
      shoppingCartItemUuid: newUuid(),
      storeUuid,
      sectionUuid: raw.sectionUuid,
      subsectionUuid: raw.subsectionUuid || '',
      title: raw.title,
      price: raw.price,
      quantity,
      imageURL: raw.imageUrl,
      specialInstructions: product.specialInstructions || '',
      customizations: U.buildCustomizations(product.customizations),
      fulfillmentIssueAction: {
        type: 'STORE_REPLACE_ITEM',
        itemSubstitutes: null,
        selectionSource: 'UBER_SUGGESTED',
        storeReplaceItem: { preferredReplacementType: 'SIMILAR_ITEM' },
      },
    };
    if (purchase.soldByUnit) {
      item.sellingOption = { soldByUnit: purchase.soldByUnit };
      if (purchase.quantityConstraintsV2) {
        item.sellingOption.quantityConstraintsV2 = purchase.quantityConstraintsV2;
      }
      item.soldByUnit = purchase.soldByUnit;
    }
    if (pricing.pricedByUnit) item.pricedByUnit = pricing.pricedByUnit;
    item.itemQuantity = {
      inSellableUnit: {
        value: { coefficient: quantity, exponent: 0 },
        measurementUnit: purchase.soldByUnit || {
          measurementType: 'MEASUREMENT_TYPE_COUNT',
          length: null,
          weight: null,
          volume: null,
        },
        measurementUnitAbbreviationText: null,
      },
      inPriceableUnit: null,
    };
    return { ok: true, item };
  };

  U.buildDeliveryAddress = async (deliveryAddressUuid) => {
    const r = await U.api('getDeliveryLocationsV2', {});
    if (!r.ok) return r;
    const buckets = (r.data && r.data.deliveryLocations) || {};
    const entries = [];
    for (const bucket of ['SAVED', 'SUGGESTED', 'TARGET']) {
      for (const e of buckets[bucket] || []) entries.push(e);
    }
    let match = null;
    for (const e of entries) {
      const loc = e.location || {};
      if (loc.id === deliveryAddressUuid) {
        match = e;
        break;
      }
    }
    if (!match) {
      return {
        ok: false,
        error: 'address_not_found',
        known: entries.map((e) => ({
          id: (e.location || {}).id,
          fullAddress: (e.location || {}).fullAddress,
        })),
      };
    }
    const loc = match.location;
    const coord = loc.coordinate || {};
    const comps = loc.addressComponents || {};
    const provider = loc.provider || 'uber_places';
    const deliveryPayload = match.deliveryPayload || {};
    const addrInfo = deliveryPayload.addressInfo || {};
    return {
      ok: true,
      deliveryAddress: {
        latitude: coord.latitude,
        longitude: coord.longitude,
        address: {
          address1: loc.addressLine1 || loc.name || '',
          address2: loc.addressLine2 || '',
          aptOrSuite: addrInfo.APT_OR_SUITE || '',
          eaterFormattedAddress: loc.fullAddress || '',
          title: loc.title || loc.name || '',
          subtitle: loc.subtitle || '',
          uuid: loc.id || '',
        },
        reference: loc.id || '',
        referenceType: provider,
        type: provider,
        addressComponents: {
          countryCode: comps.COUNTRY_CODE || '',
          firstLevelSubdivisionCode: comps.FIRST_LEVEL_SUBDIVISION_CODE || '',
          city: comps.CITY || '',
          postalCode: comps.POSTAL_CODE || '',
        },
      },
    };
  };

  U.resolveDraft = async (draftOrderUuid) => {
    const want = String(draftOrderUuid || '').trim();
    const list = await U.drafts();
    if (!list.ok) return list;
    const all = list.drafts || [];
    const withItems = list.withItems || [];

    let draft = null;
    if (want) {
      draft = withItems.find((d) => d.uuid === want) || all.find((d) => d.uuid === want);
      if (!draft) {
        const byId = await U.api('getDraftOrderByUuidV2', { draftOrderUUID: want });
        if (byId.ok) {
          draft = (byId.data && byId.data.draftOrder) || byId.data;
          if (draft && !draft.uuid) draft.uuid = want;
        }
      }
      if (!draft) {
        return {
          ok: false,
          error: 'draft_not_found',
          known: withItems.map((d) => ({
            uuid: d.uuid,
            store: (d.storeInfo || {}).title,
          })),
        };
      }
      return { ok: true, draft, draftOrderUuid: want };
    }
    if (!withItems.length) return { ok: false, error: 'no_drafts' };
    if (withItems.length > 1) {
      return {
        ok: false,
        error: 'multiple_drafts',
        known: withItems.map((d) => ({
          uuid: d.uuid,
          store: (d.storeInfo || {}).title,
          diningMode: d.diningMode || d.fulfillmentType,
          items: ((d.shoppingCart || {}).items || []).map((i) => i.title),
        })),
      };
    }
    draft = withItems[0];
    return { ok: true, draft, draftOrderUuid: draft.uuid };
  };

  U.fetchCheckoutPresentation = async (draftOrderUuid) => {
    const r = await U.api('getCheckoutPresentationV1', {
      payloadTypes: CHECKOUT_PAYLOAD_TYPES,
      draftOrderUUID: draftOrderUuid,
      isGroupOrder: false,
      clientFeaturesData: {
        paymentSelectionContext: {
          value: JSON.stringify({ deviceContext: { thirdPartyApplications: [] } }),
        },
      },
      webGiftingPersonalizationEnabled: false,
    });
    if (!r.ok) return r;
    return { ok: true, presentation: r.data };
  };

  const paymentDisplayFromPresentation = (payloads) => {
    const barLabel = (bar) => {
      if (!bar || typeof bar !== 'object') return { label: null, icon: null };
      const label = richText(bar.title);
      let icon = null;
      try {
        icon =
          ((((bar.leadingContent || {}).illustrationContent || {}).illustration || {})
            .illustration || {}).icon || {};
        icon = (icon.icon || {}).icon;
      } catch (_) {
        icon = null;
      }
      return { label, icon: typeof icon === 'string' ? icon : null };
    };
    const nz = payloads.neutralZonePayload || {};
    for (const row of nz.neutralZoneContentRows || []) {
      if (!row || typeof row !== 'object') continue;
      const { label, icon } = barLabel(row.paymentBar);
      if (label) return { label, icon };
    }
    return barLabel(payloads.paymentBarPayload);
  };

  U.parseCheckoutConfirmation = (presentation, draft) => {
    draft = draft || {};
    const payloads = presentation.checkoutPayloads || {};
    const drafts = presentation.draftOrders || [];
    const draft0 = drafts[0] || {};

    let diningMode = (
      draft.diningMode ||
      draft0.diningMode ||
      draft.fulfillmentType ||
      draft0.fulfillmentType ||
      'DELIVERY'
    ).toUpperCase();
    const isPickup = diningMode === 'PICKUP';

    const etaPayload = payloads.eta || {};
    const etaPrefix =
      etaPayload.prefixText || (isPickup ? 'Pickup time' : 'Delivery time');
    const etaRange = etaPayload.rangeText;

    const loc = payloads.locationInfo || {};
    const addr = loc.address || {};
    const instruction = loc.instruction || {};
    const storeName = addr.title || (draft.storeInfo || {}).title;
    const storeAddress = richText(addr.subtitle) || richText(addr);
    const distance = richText(instruction.subtitle);

    const cartPayload = payloads.cartItems || {};
    let itemsOut = [];
    for (const ci of cartPayload.cartItems || []) {
      const charges =
        (cartPayload.itemCharges || {})[ci.shoppingCartItemUUID || ''] || {};
      let coeff = ((ci.quantity || {}).value || {}).coefficient;
      let qty = 1;
      if (coeff && typeof coeff === 'object') qty = coeff.low != null ? coeff.low : 1;
      else if (coeff != null) qty = coeff;
      itemsOut.push({
        name: richText(ci.title),
        quantity: qty,
        price: richText(ci.originalPrice) || charges.originalAmount,
        image: ci.imageUrl,
        itemUuid: charges.itemUUID,
        shoppingCartItemUuid: ci.shoppingCartItemUUID,
      });
    }
    if (!itemsOut.length) {
      for (const i of (draft.shoppingCart || {}).items || []) {
        const priceCents = i.price;
        itemsOut.push({
          name: i.title,
          quantity: i.quantity != null ? i.quantity : 1,
          price:
            typeof priceCents === 'number' ? `$${(priceCents / 100).toFixed(2)}` : null,
          image: i.imageURL,
          itemUuid: i.skuUUID || i.uuid,
        });
      }
    }

    const fare = payloads.fareBreakdown || {};
    const chargesOut = [];
    for (const c of fare.charges || []) {
      chargesOut.push({
        label: richText(c.title) || (c.title || {}).text,
        amount: richText(c.value) || (c.value || {}).text,
        fareInfoId: ((c.fareBreakdownChargeMetadata || {}).fareInfoID),
      });
    }

    const totalPayload = payloads.total || {};
    const totalObj = totalPayload.total || {};
    const totalValue = totalObj.value || {};
    let totalE5 = totalValue.amountE5;
    const currency =
      totalValue.currencyCode || draft.currencyCode || 'USD';
    const totalFormatted = totalObj.formattedValue;
    const totalAmount = amountE5(totalE5);
    if (totalE5 && typeof totalE5 === 'object') totalE5 = totalE5.low;

    const paymentUuid =
      draft.paymentProfileUUID || draft0.paymentProfileUUID || '';
    const payDisp = paymentDisplayFromPresentation(payloads);

    const delivery = draft.deliveryAddress || {};
    const deliveryAddr = delivery.address || {};
    const storeInfo = draft.storeInfo || {};
    const storeLoc = storeInfo.location || {};
    const storeAddrObj = storeLoc.address || {};

    return {
      draftOrderUuid: draft.uuid || draft0.uuid,
      diningMode,
      fulfillmentType: (draft.fulfillmentType || diningMode || '').toUpperCase(),
      isPickup,
      store: {
        name: storeName || storeInfo.title,
        address: storeAddress || storeAddrObj.eaterFormattedAddress,
        distance,
        uuid:
          draft.storeUuid ||
          draft.restaurantUUID ||
          storeInfo.uuid,
      },
      eta: { label: etaPrefix, range: etaRange },
      items: itemsOut,
      fareBreakdown: chargesOut,
      total: totalFormatted,
      totalAmount,
      currency,
      payment: {
        paymentProfileUuid: paymentUuid,
        display: payDisp.label,
        icon: payDisp.icon,
      },
      deliveryAddress: deliveryAddr
        ? {
            fullAddress:
              deliveryAddr.eaterFormattedAddress || deliveryAddr.title,
            latitude: delivery.latitude,
            longitude: delivery.longitude,
          }
        : null,
      _checkout: {
        totalAmountE5: totalE5,
        paymentProfileUuid: paymentUuid,
      },
    };
  };

  U.addToCart = async (opts) => {
    opts = opts || {};
    const storeUuid = opts.storeUuid || '';
    const items = opts.items || [];
    const deliveryAddressUuid = opts.deliveryAddressUuid || '';
    const currencyCode = opts.currencyCode || 'USD';
    let mode = (opts.diningMode || 'DELIVERY').toUpperCase();
    if (mode !== 'DELIVERY' && mode !== 'PICKUP') {
      return { ok: false, error: 'bad_dining_mode', diningMode: opts.diningMode };
    }
    if (!items.length) return { ok: false, error: 'no_items' };
    if (mode === 'DELIVERY' && !deliveryAddressUuid) {
      return { ok: false, error: 'delivery_address_required' };
    }

    let deliveryAddress = null;
    if (deliveryAddressUuid) {
      const addr = await U.buildDeliveryAddress(deliveryAddressUuid);
      if (!addr.ok) return addr;
      deliveryAddress = addr.deliveryAddress;
    }

    const drafts = await U.drafts();
    if (drafts.ok) {
      for (const d of drafts.drafts || []) {
        if (d.storeUuid === storeUuid || d.restaurantUUID === storeUuid) {
          await U.api('discardDraftOrderV2', { draftOrderUUID: d.uuid });
        }
      }
    }

    const cartItems = [];
    for (const product of items) {
      const built = U.buildCartItem(product, storeUuid);
      if (!built.ok) return built;
      cartItems.push(built.item);
    }

    const created = await U.api('createDraftOrderV2', {
      isMulticart: true,
      shoppingCartItems: cartItems,
      removeAdapters: true,
      useCredits: true,
      extraPaymentProfiles: [],
      promotionOptions: {
        autoApplyPromotionUUIDs: [],
        selectedPromotionInstanceUUIDs: [],
        skipApplyingPromotion: false,
      },
      deliveryTime: { asap: true },
      deliveryType: 'ASAP',
      currencyCode,
      interactionType: 'door_to_door',
      checkMultipleDraftOrdersCap: true,
      actionMeta: { isQuickAdd: true },
      analyticsRelevantData: { profileSource: '' },
      businessDetails: {},
    });
    if (!created.ok) return created;

    let draft = (created.data && created.data.draftOrder) || created.data || {};
    const draftUuid = draft.uuid || '';
    let updated = draft;

    if (deliveryAddress) {
      const up = await U.api('updateDraftOrderV2', {
        draftOrderUUID: draftUuid,
        deliveryAddress,
        removeAdapters: true,
      });
      if (!up.ok) return up;
      updated = (up.data && up.data.draftOrder) || up.data || updated;
    }

    if (mode === 'PICKUP') {
      const up = await U.api('updateDraftOrderV2', {
        draftOrderUUID: draftUuid,
        diningMode: 'PICKUP',
        fulfillmentType: 'PICKUP',
        removeAdapters: true,
      });
      if (!up.ok) return up;
      updated = (up.data && up.data.draftOrder) || up.data || updated;
    }

    const cart = updated.shoppingCart || draft.shoppingCart || {};
    const finalItems = cart.items || [];
    const finalAddress = updated.deliveryAddress || draft.deliveryAddress || {};
    const finalAddressObj = finalAddress.address || {};
    const finalMode = (
      updated.diningMode ||
      updated.fulfillmentType ||
      mode
    ).toUpperCase();
    const totalCents = finalItems.reduce(
      (s, i) => s + (i.price || 0) * (i.quantity || 1),
      0,
    );
    const draftCurrency =
      (created.data && created.data.currencyCode) ||
      draft.currencyCode ||
      currencyCode;

    return {
      ok: true,
      draft: {
        id: draftUuid,
        name: `Cart (${finalItems.length} items)`,
        orderId: draftUuid,
        status: 'draft',
        diningMode: finalMode,
        fulfillmentType: (updated.fulfillmentType || finalMode).toUpperCase(),
        isPickup: finalMode === 'PICKUP',
        totalAmount: totalCents ? totalCents / 100 : null,
        currency: draftCurrency,
        contains: finalItems.map((i) => ({
          shape: 'product',
          id: i.uuid,
          name: i.title,
          image: i.imageURL,
          priceAmount: i.price ? i.price / 100 : null,
          currency: draftCurrency,
          quantity: i.quantity || 1,
        })),
        shipped_to:
          finalAddress && finalMode !== 'PICKUP'
            ? {
                shape: 'place',
                fullAddress:
                  finalAddressObj.eaterFormattedAddress || finalAddressObj.title,
                latitude: finalAddress.latitude,
                longitude: finalAddress.longitude,
              }
            : null,
        purchased_at:
          finalMode === 'PICKUP'
            ? {
                shape: 'place',
                id: storeUuid,
                name: (updated.storeInfo || draft.storeInfo || {}).title,
                featureType: 'poi',
              }
            : null,
      },
    };
  };

  U.setDiningMode = async (draftOrderUuid, diningMode) => {
    const mode = String(diningMode || '').toUpperCase();
    if (mode !== 'DELIVERY' && mode !== 'PICKUP') {
      return { ok: false, error: 'bad_dining_mode' };
    }
    const resp = await U.api('updateDraftOrderV2', {
      draftOrderUUID: draftOrderUuid,
      diningMode: mode,
      fulfillmentType: mode,
      removeAdapters: true,
    });
    if (!resp.ok) return resp;
    const draft = (resp.data && resp.data.draftOrder) || resp.data || {};
    const final = (draft.diningMode || draft.fulfillmentType || mode).toUpperCase();
    return {
      ok: true,
      draftOrderUuid,
      diningMode: final,
      fulfillmentType: (draft.fulfillmentType || final).toUpperCase(),
      isPickup: final === 'PICKUP',
    };
  };

  U.getCarts = async () => {
    const list = await U.drafts();
    if (!list.ok) return list;
    const orders = [];
    for (const draft of list.drafts || []) {
      const cart = draft.shoppingCart || {};
      const items = cart.items || [];
      if (!items.length) continue;
      const store = draft.storeInfo || {};
      const totalCents = items.reduce(
        (s, i) => s + (i.price || 0) * (i.quantity || 1),
        0,
      );
      const cartCurrency = draft.currencyCode;
      const diningMode = (
        draft.diningMode ||
        draft.fulfillmentType ||
        'DELIVERY'
      ).toUpperCase();
      const delivery = draft.deliveryAddress || {};
      const deliveryAddr = delivery.address || {};
      const storeLoc = store.location || {};
      const storeAddr = storeLoc.address || {};
      orders.push({
        id: draft.uuid,
        name: store.title || draft.storeUuid || 'Unknown store',
        published: draft.createdAt,
        status: 'draft',
        diningMode,
        fulfillmentType: (draft.fulfillmentType || diningMode).toUpperCase(),
        isPickup: diningMode === 'PICKUP',
        paymentProfileUuid: draft.paymentProfileUUID,
        totalAmount: totalCents ? totalCents / 100 : null,
        currency: cartCurrency,
        purchased_at:
          store.title || draft.storeUuid || draft.restaurantUUID
            ? {
                shape: 'place',
                id: draft.storeUuid || draft.restaurantUUID,
                name: store.title,
                image: store.heroImageUrl,
                fullAddress: storeAddr.eaterFormattedAddress,
                featureType: 'poi',
              }
            : null,
        shipped_to:
          deliveryAddr && diningMode !== 'PICKUP'
            ? {
                shape: 'place',
                fullAddress:
                  deliveryAddr.eaterFormattedAddress || deliveryAddr.title,
                latitude: delivery.latitude,
                longitude: delivery.longitude,
                featureType: 'address',
              }
            : null,
        contains: items.map((i) => {
          const row = {
            shape: 'product',
            id: i.skuUUID || i.uuid,
            name: i.title,
            image: i.imageURL,
            quantity: i.quantity || 1,
            priceAmount: i.price ? i.price / 100 : null,
            currency: cartCurrency,
          };
          if (i.specialInstructions) row.specialInstructions = i.specialInstructions;
          return row;
        }),
      });
    }
    return { ok: true, carts: orders };
  };

  U.previewCheckout = async (draftOrderUuid) => {
    const resolved = await U.resolveDraft(draftOrderUuid);
    if (!resolved.ok) return resolved;
    const pres = await U.fetchCheckoutPresentation(resolved.draftOrderUuid);
    if (!pres.ok) return pres;
    const confirmation = U.parseCheckoutConfirmation(
      pres.presentation,
      resolved.draft,
    );
    return { ok: true, confirmation };
  };

  U.clearCart = async (draftOrderUuid) => {
    const r = await U.api('discardDraftOrderV2', {
      draftOrderUUID: draftOrderUuid,
    });
    if (!r.ok) return r;
    return { ok: true, status: 'cleared', draftOrderUuid };
  };

  const fareBreakdownCharges = (presentation) => {
    const fare = ((presentation.checkoutPayloads || {}).fareBreakdown) || {};
    const out = [];
    for (const c of fare.charges || []) {
      const meta = c.fareBreakdownChargeMetadata || {};
      const analytics = meta.analyticsInfo || c.analyticsInfo || {};
      const charge = {
        fareInfoID: meta.fareInfoID || analytics.fareInfoID,
        label: richText(c.title) || (c.title || {}).text,
        amount: richText(c.value) || (c.value || {}).text,
      };
      if (analytics && Object.keys(analytics).length) charge.analyticsInfo = analytics;
      const cleaned = {};
      Object.keys(charge).forEach((k) => {
        if (charge[k] != null) cleaned[k] = charge[k];
      });
      out.push(cleaned);
    }
    return out;
  };

  const cartItemsForCheckout = (draft, presentation) => {
    const items = [];
    for (const i of ((draft.shoppingCart || {}).items) || []) {
      items.push({
        uuid: i.uuid || i.skuUUID,
        shoppingCartItemUuid: i.shoppingCartItemUuid || i.shoppingCartItemUUID,
        title: i.title,
        quantity: i.quantity != null ? i.quantity : 1,
        price: i.price,
        storeUuid: i.storeUuid || draft.storeUuid,
      });
    }
    if (items.length) return items;
    const cartPayload = ((presentation.checkoutPayloads || {}).cartItems) || {};
    for (const ci of cartPayload.cartItems || []) {
      const charges =
        (cartPayload.itemCharges || {})[ci.shoppingCartItemUUID || ''] || {};
      let coeff = ((ci.quantity || {}).value || {}).coefficient;
      let qty = 1;
      if (coeff && typeof coeff === 'object') qty = coeff.low != null ? coeff.low : 1;
      else if (coeff != null) qty = coeff;
      items.push({
        uuid: charges.itemUUID,
        shoppingCartItemUuid: ci.shoppingCartItemUUID,
        title: richText(ci.title),
        quantity: qty,
      });
    }
    return items;
  };

  const findUseCaseKey = (obj, found) => {
    if (found.v || obj == null) return;
    if (typeof obj === 'object') {
      if (!Array.isArray(obj)) {
        if (typeof obj.useCaseKey === 'string' && obj.useCaseKey) {
          found.v = obj.useCaseKey;
          return;
        }
        Object.keys(obj).forEach((k) => findUseCaseKey(obj[k], found));
      } else {
        obj.forEach((v) => findUseCaseKey(v, found));
      }
    }
  };

  /** Place order — caller (Python) must have human go after preview. */
  U.checkout = async (draftOrderUuid) => {
    const resolved = await U.resolveDraft(draftOrderUuid);
    if (!resolved.ok) return resolved;
    const draft = resolved.draft;
    draftOrderUuid = resolved.draftOrderUuid;

    const pres = await U.fetchCheckoutPresentation(draftOrderUuid);
    if (!pres.ok) return pres;
    const presentation = pres.presentation;
    const drafts = presentation.draftOrders || [];
    const draft0 = drafts[0] || {};

    const paymentUuid =
      draft.paymentProfileUUID || draft0.paymentProfileUUID || '';
    let storeUuid =
      draft.storeUuid ||
      draft.restaurantUUID ||
      (draft.storeInfo || {}).uuid ||
      draft0.storeUuid ||
      '';
    let diningMode = (
      draft.diningMode ||
      draft.fulfillmentType ||
      draft0.diningMode ||
      draft0.fulfillmentType ||
      'DELIVERY'
    ).toUpperCase();
    if (diningMode !== 'DELIVERY' && diningMode !== 'PICKUP') diningMode = 'DELIVERY';

    let storeTitle = (draft.storeInfo || {}).title || '';
    let cityName = '';
    let eaterConsent = {
      defaultOptIn: false,
      eaterConsented: false,
      orgUUID: '',
      optIn: { dialogText: '', infoText: '' },
      optOut: { infoText: '', headerText: '', bodyText: '' },
    };
    if (storeUuid) {
      const storeRes = await U.store(storeUuid);
      if (storeRes.ok && storeRes.store) {
        const store = storeRes.store;
        cityName = (store.citySlug || store.cityName || '').toLowerCase();
        storeTitle = store.title || storeTitle;
        if (store.eaterConsent && typeof store.eaterConsent === 'object') {
          eaterConsent = Object.assign({}, eaterConsent, store.eaterConsent);
        }
      }
    }
    if (!cityName) {
      cityName = (
        draft.storeCitySlug ||
        draft.citySlug ||
        (draft.storeInfo || {}).citySlug ||
        ''
      ).toLowerCase();
    }

    const totalPayload = ((presentation.checkoutPayloads || {}).total) || {};
    const totalObj = totalPayload.total || {};
    const totalValue = totalObj.value || {};
    let totalE5 = totalValue.amountE5 || 0;
    if (totalE5 && typeof totalE5 === 'object') totalE5 = totalE5.low || 0;
    const currency =
      totalValue.currencyCode || draft.currencyCode || 'USD';

    const found = { v: '' };
    findUseCaseKey(presentation, found);
    const useCaseKey = found.v;

    const missing = [];
    if (!storeUuid) missing.push('storeUuid');
    if (!paymentUuid) missing.push('paymentProfileUuid');
    if (!totalE5) missing.push('orderTotalFare');
    if (missing.length) {
      return {
        ok: false,
        error: 'checkout_missing_fields',
        missing,
        draftOrderUuid,
        diningMode,
      };
    }

    const fareCharges = fareBreakdownCharges(presentation);
    const cartItems = cartItemsForCheckout(draft, presentation);
    const checkoutSession = newUuid();
    const actionValue = JSON.stringify({
      checkoutSessionUUID: checkoutSession,
      useCaseKey,
      actionResults: [],
      estimatedPaymentPlan: {
        defaultPaymentProfile: {
          paymentProfileUUID: paymentUuid,
          currencyAmount: { amountE5: totalE5, currencyCode: currency },
        },
        useCredits: true,
      },
    });

    const tz =
      Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/Chicago';

    const checkoutBody = {
      draftOrderUUID: draftOrderUuid,
      storeInstructions: '',
      extraPaymentData: '',
      shareCPFWithRestaurant: false,
      extraParams: {
        timezone: tz,
        trackingCode: null,
        storeUuid,
        cityName,
        paymentIntent: 'personal',
        isTealiumEnabled: false,
        paymentProfileTokenType: 'braintree',
        paymentProfileUuid: paymentUuid,
        isNeutralZoneEnabled: true,
        isScheduledOrder: false,
        isBillSplitOrder: false,
        isDraftOrderParticipant: false,
        isEditScheduledOrder: false,
        orderTotalFare: totalE5,
        orderCurrency: currency,
        verticalLabel: 'RESTAURANT',
        cookieConsent: true,
        checkoutType: 'drafting',
        isAddOnOrder: false,
        isMatchbox: false,
        promotionUuid: '',
        fareBreakdownCharges: fareCharges,
        cartItems,
        cartItemsCount: cartItems.length,
      },
      currentEaterConsent: eaterConsent,
      newEaterConsented: false,
      isGroupOrder: false,
      bypassAuthDeclineForTrustedUser: false,
      checkoutActionResultParams: { value: actionValue },
      orderRequestedEventMetadata: {
        placeOrderClick: {
          trackingCode: '',
          orderUuid: draftOrderUuid,
          store: {
            title: storeTitle,
            uuid: storeUuid,
            currencyCode: currency,
            status: 'OPEN',
            isVirtualized: false,
            locationType: 'PHYSICAL',
            isLost: false,
          },
          diningMode,
          bypassAuthDeclineForTrustedUser: false,
          paymentIntent: 'personal',
          checkoutType: 'drafting',
          isBillSplitOrder: false,
          isEditScheduledOrder: false,
          isDraftOrderParticipant: false,
          isGroupOrder: false,
          deliveryType: 'ASAP',
          isScheduled: false,
          storeUuid,
          isBundle: false,
          isAddOnOrder: false,
          modality: diningMode,
          checkoutActionResultsValue: actionValue,
        },
        promotionUuid: '',
      },
      skipOrderRequestedEvent: false,
    };

    const placed = await U.api('checkoutOrdersByDraftOrdersV1', checkoutBody);
    if (!placed.ok) return placed;

    const totalAmount = totalE5 ? totalE5 / 1e5 : null;
    return {
      ok: true,
      order: {
        id: draftOrderUuid,
        orderId: draftOrderUuid,
        name: totalAmount
          ? `Order placed ($${totalAmount.toFixed(2)})`
          : 'Order placed',
        status: 'placed',
        diningMode,
        isPickup: diningMode === 'PICKUP',
        total: totalObj.formattedValue,
        totalAmount,
        currency,
        _checkoutResponse: placed.data || null,
      },
    };
  };

  // Patch help surface
  const prevHelp = U.help;
  U.help = () => {
    const base = typeof prevHelp === 'function' ? prevHelp() : { v: U.__v, methods: [] };
    return {
      v: base.v,
      cartV: U.__cartV,
      methods: (base.methods || []).concat([
        'addToCart(opts)',
        'setDiningMode(uuid, mode)',
        'getCarts()',
        'previewCheckout(uuid?)',
        'clearCart(uuid)',
        'checkout(uuid)',
        'resolveDraft(uuid?)',
      ]),
    };
  };
})();
