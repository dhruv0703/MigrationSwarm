package com.example.support.users;

public interface UserDirectory {
    SupportUser requireActive(String externalId);
}
