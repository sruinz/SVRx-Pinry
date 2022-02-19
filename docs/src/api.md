# API Support

Pinry support API accessing via token so that you could write your own client or import any pin format to your instance.

# Get Token
Just go to `My -> Profile` page to get the token.


# Access API via Token

Here is an example for curl to access `user-profile` API:

    curl -X GET http://192.168.1.101:8080/api/v2/profile/users/ -H 'Authorization: Token YOUR_API_TOKEN'

You will get response like this:

    [
        {
        "username": "winkidney",
        "token": "YOUR_API_TOKEN",
        "email": "winkidney@gmail.com",
        "gravatar": "0d7161ac663cdb21108502cd4051149c",
        "resource_link": "http://localhost:8000/api/v2/profile/users/1/"
        }
    ]


# API reference
Just use the interactive API interface by DRF or follow the file [api.js](https://github.com/pinry/pinry/blob/master/pinry-spa/src/components/api.js)
