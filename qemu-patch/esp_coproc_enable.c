/*
 * esp_coproc_enable -- leave coprocessors enabled out of reset.
 *
 * A declared inaccuracy, opt-in per board (ADR 0013). Xtensa resets CPENABLE to
 * zero and QEMU models that faithfully, but the T-Deck's vendor firmware runs a
 * float in a C++ global constructor -- before FreeRTOS exists, so before any
 * task owns the FPU state. IDF has no save area to hand at that point and
 * panics with "Coprocessor exception". The firmware never writes CPENABLE
 * itself; it simply expects the FPU to be on already.
 *
 * We cannot confirm from here what silicon resets CPENABLE to, so this stays
 * off unless a board asks for it, and the board file is where the claim is
 * recorded. The blast radius is small by construction: FreeRTOS writes CPENABLE
 * on every context switch, so this value survives only until the first task
 * runs.
 *
 * This is the one file of ours under hw/xtensa rather than hw/misc, because
 * only target-specific sources may include the target's cpu.h.
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/qdev-properties.h"
#include "hw/qdev-core.h"
#include "hw/core/cpu.h"
#include "qom/object.h"
#include "sysemu/reset.h"
#include "target/xtensa/cpu.h"

#define TYPE_ESP_COPROC_ENABLE "esp-coproc-enable"

OBJECT_DECLARE_SIMPLE_TYPE(EspCoprocEnable, ESP_COPROC_ENABLE)

struct EspCoprocEnable {
    DeviceState parent_obj;
    uint32_t mask;
};

static void coproc_enable_reset(void *opaque)
{
    EspCoprocEnable *s = ESP_COPROC_ENABLE(opaque);
    CPUState *cs;

    /* Both cores: either may be the one running startup after a soft reset.
     * No tb_flush is needed -- CPENABLE feeds the TB lookup flags, which are
     * recomputed per lookup, so a stale block cannot be reused. */
    CPU_FOREACH(cs) {
        CPUXtensaState *env = &XTENSA_CPU(cs)->env;
        env->sregs[CPENABLE] = s->mask;
    }
}

static void coproc_enable_realize(DeviceState *dev, Error **errp)
{
    EspCoprocEnable *s = ESP_COPROC_ENABLE(dev);

    if (s->mask == 0) {
        error_setg(errp, "esp-coproc-enable: mask=0 would do nothing; "
                         "omit the device instead");
        return;
    }

    /* Registered here rather than by the machine, and that placement is the
     * whole ordering argument. The SoC registers the handler that calls
     * cpu_reset() during machine init; -device instances are created afterwards
     * in qemu_create_cli_devices(). Handlers run in registration order, so this
     * one runs after the CPU has been reset and its write survives. */
    qemu_register_reset(coproc_enable_reset, s);
}

static Property coproc_enable_props[] = {
    /* Bit per coprocessor. 0x1 is CP0, the FPU, which is all the evidence in
     * ADR 0013 supports; anything wider is a claim someone has to justify. */
    DEFINE_PROP_UINT32("mask", EspCoprocEnable, mask, 0x1),
    DEFINE_PROP_END_OF_LIST(),
};

static void coproc_enable_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    dc->realize = coproc_enable_realize;
    dc->user_creatable = true;
    device_class_set_props(dc, coproc_enable_props);
}

static const TypeInfo coproc_enable_info = {
    .name = TYPE_ESP_COPROC_ENABLE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(EspCoprocEnable),
    .class_init = coproc_enable_class_init,
};

static void coproc_enable_register_types(void)
{
    type_register_static(&coproc_enable_info);
}

type_init(coproc_enable_register_types)
