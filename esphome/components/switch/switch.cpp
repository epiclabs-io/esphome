#include "switch.h"
#include "esphome/core/log.h"

namespace esphome {
namespace switch_ {

static const char *const TAG = "switch";

Switch::Switch(const std::string &name) : EntityBase(name), state(false) {}
Switch::Switch() : Switch("") {}

void Switch::turn_on() {
  ESP_LOGD(TAG, "'%s' Turning ON.", this->get_name().c_str());
  this->write_state(!this->inverted_);
}
void Switch::turn_off() {
  ESP_LOGD(TAG, "'%s' Turning OFF.", this->get_name().c_str());
  this->write_state(this->inverted_);
}
void Switch::toggle() {
  ESP_LOGD(TAG, "'%s' Toggling %s.", this->get_name().c_str(), this->state ? "OFF" : "ON");
  this->write_state(this->inverted_ == this->state);
}
optional<bool> Switch::get_initial_state() {
  if (!is_restore_mode_persistent())
    return {};

  this->rtc_ = global_preferences->make_preference<bool>(this->get_object_id_hash());
  bool initial_state;
  if (!this->rtc_.load(&initial_state))
    return {};
  return initial_state;
}
bool Switch::get_initial_state_with_restore_mode() {
  bool initial_state = false;
  switch (this->restore_mode_) {
    case SWITCH_RESTORE_DEFAULT_OFF:
      initial_state = this->get_initial_state().value_or(false);
      break;
    case SWITCH_RESTORE_DEFAULT_ON:
      initial_state = this->get_initial_state().value_or(true);
      break;
    case SWITCH_RESTORE_INVERTED_DEFAULT_OFF:
      initial_state = !this->get_initial_state().value_or(true);
      break;
    case SWITCH_RESTORE_INVERTED_DEFAULT_ON:
      initial_state = !this->get_initial_state().value_or(false);
      break;
    case SWITCH_ALWAYS_OFF:
      initial_state = false;
      break;
    case SWITCH_ALWAYS_ON:
      initial_state = true;
      break;
  }

  return initial_state;
}
void Switch::publish_state(bool state) {
  if (!this->publish_dedup_.next(state))
    return;
  this->state = state != this->inverted_;

  if (is_restore_mode_persistent())
    this->rtc_.save(&this->state);

  ESP_LOGD(TAG, "'%s': Sending state %s", this->name_.c_str(), ONOFF(this->state));
  this->state_callback_.call(this->state);
}
bool Switch::assumed_state() { return false; }

bool Switch::is_restore_mode_persistent() {
  return restore_mode_ == SWITCH_ALWAYS_OFF || restore_mode_ == SWITCH_ALWAYS_ON;
}

void Switch::add_on_state_callback(std::function<void(bool)> &&callback) {
  this->state_callback_.add(std::move(callback));
}
void Switch::set_inverted(bool inverted) { this->inverted_ = inverted; }
bool Switch::is_inverted() const { return this->inverted_; }

std::string Switch::get_device_class() {
  if (this->device_class_.has_value())
    return *this->device_class_;
  return "";
}
void Switch::set_device_class(const std::string &device_class) { this->device_class_ = device_class; }

}  // namespace switch_
}  // namespace esphome
