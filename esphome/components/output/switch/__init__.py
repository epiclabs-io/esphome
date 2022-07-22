import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import output, switch
from esphome.const import CONF_OUTPUT, CONF_RESTORE_MODE
from .. import output_ns

OutputSwitch = output_ns.class_("OutputSwitch", switch.Switch, cg.Component)

OutputSwitchRestoreMode = output_ns.enum("OutputSwitchRestoreMode")


CONFIG_SCHEMA = (
    switch.switch_schema(OutputSwitch)
    .extend(
        {
            cv.Required(CONF_OUTPUT): cv.use_id(output.BinaryOutput),
            cv.Optional(CONF_RESTORE_MODE, default="RESTORE_DEFAULT_OFF"): cv.enum(
                switch.RESTORE_MODES, upper=True, space="_"
            ),
        }
    )
    .extend(cv.COMPONENT_SCHEMA)
)


async def to_code(config):
    var = await switch.new_switch(config)
    await cg.register_component(var, config)

    output_ = await cg.get_variable(config[CONF_OUTPUT])
    cg.add(var.set_output(output_))
